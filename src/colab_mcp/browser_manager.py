"""Playwright-backed browser lifecycle for Google Colab."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

COLAB_ORIGIN = "https://colab.research.google.com"
_ALLOWED_COLAB_HOSTS = {"colab.research.google.com", "colab.google.com"}
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


class BrowserNavigationError(RuntimeError):
    """A token-safe browser navigation failure."""


class BrowserLaunchError(RuntimeError):
    """A browser startup failure with actionable, token-safe context."""


@dataclass(frozen=True)
class BrowserConfig:
    profile_dir: Path
    executable_path: Path | None = None
    headless: bool = False
    channel: str | None = None


@dataclass(frozen=True)
class ColabTab:
    title: str
    url: str
    selected: bool = False


@dataclass(frozen=True)
class BrowserStatus:
    running: bool
    profile_dir: str
    executable_path: str | None
    headless: bool
    tab_count: int
    colab_tab_count: int
    channel: str | None = None


async def _start_playwright():
    from playwright.async_api import async_playwright

    return await async_playwright().start()


class ColabBrowserManager:
    """Own a Playwright persistent context dedicated to Colab automation."""

    def __init__(
        self,
        config: BrowserConfig,
        playwright_starter: Callable[[], Awaitable[Any]] | None = None,
    ):
        self.config = config
        self._playwright_starter = playwright_starter or _start_playwright
        self._playwright = None
        self._context = None
        self._start_lock = asyncio.Lock()
        self._open_lock = asyncio.Lock()

    async def ensure_started(self):
        if self._context is not None:
            return self._context

        async with self._start_lock:
            if self._context is not None:
                return self._context

            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            self.config.profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await self._playwright_starter()
            launch_options: dict[str, Any] = {"headless": self.config.headless}
            if self.config.executable_path is not None:
                launch_options["executable_path"] = str(self.config.executable_path)
            elif self.config.channel is not None:
                launch_options["channel"] = self.config.channel
            try:
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        str(self.config.profile_dir),
                        **launch_options,
                    )
                )
                context = self._context

                def mark_closed(*_args) -> None:
                    if self._context is context:
                        self._context = None

                if hasattr(context, "on"):
                    context.on("close", mark_closed)
            except Exception:
                playwright, self._playwright = self._playwright, None
                if playwright is not None:
                    try:
                        await playwright.stop()
                    except Exception:
                        pass
                raise BrowserLaunchError(
                    "Failed to launch the persistent browser profile at "
                    f"{self.config.profile_dir}. Ensure the profile is not already in use "
                    "and the configured Chrome executable or channel is installed."
                ) from None
            return self._context

    async def open_notebook(
        self,
        target: str,
        *,
        connection_port: int | None = None,
        connection_token: str | None = None,
    ) -> ColabTab:
        normalized = normalize_notebook_target(target)
        async with self._open_lock:
            return await self._open_notebook(
                normalized,
                connection_port=connection_port,
                connection_token=connection_token,
            )

    async def _open_notebook(
        self,
        target: str,
        *,
        connection_port: int | None = None,
        connection_token: str | None = None,
    ) -> ColabTab:
        context = await self.ensure_started()
        normalized = normalize_notebook_target(target)
        navigation_url = normalized
        if connection_port is not None or connection_token is not None:
            if connection_port is None or connection_token is None:
                raise ValueError(
                    "connection_port and connection_token must be provided together"
                )
            navigation_url = build_mcp_connection_url(
                normalized,
                port=connection_port,
                token=connection_token,
            )

        target_parts = urlsplit(normalized)
        page = next(
            (
                candidate
                for candidate in context.pages
                if _same_notebook(candidate.url, target_parts)
            ),
            None,
        )
        created_page = page is None
        if page is None:
            page = await context.new_page()

        try:
            await page.goto(navigation_url, wait_until="domcontentloaded")
            await page.bring_to_front()
            title = await page.title()
        except Exception:
            if created_page:
                try:
                    await page.close()
                except Exception:
                    pass
            safe_url = _redact_browser_url(navigation_url)
            raise BrowserNavigationError(
                f"Failed to open Colab notebook at {safe_url}"
            ) from None
        return ColabTab(
            title=title,
            url=_redact_browser_url(page.url),
            selected=True,
        )

    async def clear_connection_fragment(self, target: str) -> None:
        """Remove an MCP fragment from the matching tab's current history entry."""
        normalized = normalize_notebook_target(target)
        context = await self.ensure_started()
        target_parts = urlsplit(normalized)
        page = next(
            (
                candidate
                for candidate in context.pages
                if _same_notebook(candidate.url, target_parts)
            ),
            None,
        )
        if page is None:
            raise BrowserNavigationError(
                "Could not find the matching Colab tab to clear connection state"
            )
        if urlsplit(page.url).fragment:
            await page.evaluate(
                "url => history.replaceState(null, '', url)",
                _redact_browser_url(page.url),
            )
            if urlsplit(page.url).fragment:
                raise BrowserNavigationError(
                    "Could not verify the Colab connection fragment was cleared"
                )

    async def list_colab_tabs(self) -> list[ColabTab]:
        context = await self.ensure_started()
        tabs: list[ColabTab] = []
        for page in context.pages:
            if not _is_colab_url(page.url):
                continue
            tabs.append(
                ColabTab(
                    title=await page.title(),
                    url=_redact_browser_url(page.url),
                )
            )
        return tabs

    def status(self) -> BrowserStatus:
        pages = self._context.pages if self._context is not None else []
        return BrowserStatus(
            running=self._context is not None,
            profile_dir=str(self.config.profile_dir),
            executable_path=(
                str(self.config.executable_path)
                if self.config.executable_path is not None
                else None
            ),
            headless=self.config.headless,
            tab_count=len(pages),
            colab_tab_count=sum(1 for page in pages if _is_colab_url(page.url)),
            channel=self.config.channel,
        )

    async def close(self) -> None:
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        try:
            if context is not None:
                await context.close()
        finally:
            if playwright is not None:
                await playwright.stop()


def _is_notebook_path(path: str) -> bool:
    normalized = path.rstrip("/")
    if normalized == "/scratchpad":
        return True
    prefix = "/drive/"
    return normalized.startswith(prefix) and bool(
        _DRIVE_ID_RE.fullmatch(normalized.removeprefix(prefix))
    )


def _is_colab_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_COLAB_HOSTS


def _same_notebook(candidate_url: str, target_parts) -> bool:
    candidate = urlsplit(candidate_url)
    return _is_colab_url(candidate_url) and candidate.path.rstrip(
        "/"
    ) == target_parts.path.rstrip("/")


def _redact_browser_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def normalize_notebook_target(target: str) -> str:
    """Normalize a Google Drive notebook id or trusted Colab URL."""
    value = target.strip()
    if _DRIVE_ID_RE.fullmatch(value):
        return f"{COLAB_ORIGIN}/drive/{value}"

    parsed = urlsplit(value)
    if (
        parsed.scheme == "https"
        and parsed.hostname in _ALLOWED_COLAB_HOSTS
        and _is_notebook_path(parsed.path)
    ):
        canonical_host = "colab.research.google.com"
        return urlunsplit(("https", canonical_host, parsed.path, parsed.query, ""))

    raise ValueError("Expected a Google Colab notebook URL or Drive ID")


def build_mcp_connection_url(target: str, *, port: int, token: str) -> str:
    """Attach the local MCP endpoint to a trusted Colab notebook URL."""
    if not 1 <= port <= 65535:
        raise ValueError("MCP proxy port must be between 1 and 65535")
    if not token.strip():
        raise ValueError("MCP proxy token must not be empty")
    normalized = normalize_notebook_target(target)
    parsed = urlsplit(normalized)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["p"] = str(port)
    fragment = urlencode({"mcpProxyToken": token, "mcpProxyPort": str(port)})
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), fragment)
    )
