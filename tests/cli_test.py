import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from colab_mcp import parse_args


def test_browser_cli_defaults_to_dedicated_visible_chrome_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))

    args = parse_args(["--log", str(tmp_path / "logs")])

    assert args.playwright_browser is True
    assert args.browser_profile_dir == str(
        Path(tmp_path) / ".local/share/hermes-colab-agent/chrome-profile"
    )
    assert args.browser_channel == "chrome"
    assert args.browser_executable is None
    assert args.browser_headless is False


def test_create_browser_manager_maps_cli_configuration(tmp_path):
    colab_mcp = importlib.import_module("colab_mcp")
    profile_dir = tmp_path / "profile"
    args = parse_args(
        [
            "--log",
            str(tmp_path / "logs"),
            "--browser-profile-dir",
            str(profile_dir),
            "--browser-channel",
            "chromium",
            "--browser-executable",
            "/opt/custom-chrome",
            "--browser-headless",
        ]
    )

    manager = colab_mcp.create_browser_manager(args)

    assert manager.config.profile_dir == profile_dir
    assert manager.config.executable_path == Path("/opt/custom-chrome")
    assert manager.config.channel == "chromium"
    assert manager.config.headless is True


@pytest.mark.asyncio
async def test_main_async_closes_managed_browser(monkeypatch, tmp_path):
    colab_mcp = importlib.import_module("colab_mcp")
    events = []

    class FakeManager:
        async def close(self):
            events.append("browser.close")

    fake_manager = FakeManager()
    args = SimpleNamespace(
        log=str(tmp_path / "logs"),
        list_running=False,
        kill_stale=False,
        enable_proxy=False,
        client_oauth_config=None,
        playwright_browser=True,
    )

    async def run_async():
        events.append("mcp.run")

    monkeypatch.setattr(colab_mcp, "parse_args", lambda _: args)
    monkeypatch.setattr(colab_mcp, "init_logger", lambda _: None)
    monkeypatch.setattr(colab_mcp, "create_browser_manager", lambda _: fake_manager)
    monkeypatch.setattr(colab_mcp.mcp, "run_async", run_async)
    monkeypatch.setattr(colab_mcp.process_registry, "prune_dead", lambda: 0)
    monkeypatch.setattr(colab_mcp.process_registry, "unregister", lambda: None)

    await colab_mcp.main_async()

    assert events == ["mcp.run", "browser.close"]


@pytest.mark.asyncio
async def test_main_async_unregisters_and_clears_manager_when_browser_close_fails(
    monkeypatch, tmp_path
):
    colab_mcp = importlib.import_module("colab_mcp")
    events = []

    class FakeManager:
        async def close(self):
            events.append("browser.close")
            raise RuntimeError("close failed")

    fake_manager = FakeManager()
    args = SimpleNamespace(
        log=str(tmp_path / "logs"),
        list_running=False,
        kill_stale=False,
        enable_proxy=False,
        client_oauth_config=None,
        playwright_browser=True,
    )

    monkeypatch.setattr(colab_mcp, "parse_args", lambda _: args)
    monkeypatch.setattr(colab_mcp, "init_logger", lambda _: None)
    monkeypatch.setattr(colab_mcp, "create_browser_manager", lambda _: fake_manager)
    monkeypatch.setattr(colab_mcp.mcp, "run_async", AsyncMock())
    monkeypatch.setattr(colab_mcp.process_registry, "prune_dead", lambda: 0)
    monkeypatch.setattr(
        colab_mcp.process_registry,
        "unregister",
        lambda: events.append("unregister"),
    )

    with pytest.raises(RuntimeError, match="close failed"):
        await colab_mcp.main_async()

    assert events == ["browser.close", "unregister"]
    assert colab_mcp._browser_manager is None
