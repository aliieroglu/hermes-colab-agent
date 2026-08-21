import asyncio
import importlib
from pathlib import Path

import pytest


def test_normalize_notebook_target_accepts_drive_id():
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    result = browser_manager.normalize_notebook_target(
        "11-SndejEz6MCYARW021CFjs5SCI9EXW"
    )

    assert result == (
        "https://colab.research.google.com/drive/11-SndejEz6MCYARW021CFjs5SCI9EXW"
    )


def test_normalize_notebook_target_accepts_colab_url_and_drops_fragment():
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    result = browser_manager.normalize_notebook_target(
        "https://colab.research.google.com/drive/abcDEF_12345?usp=sharing#private"
    )

    assert result == (
        "https://colab.research.google.com/drive/abcDEF_12345?usp=sharing"
    )


def test_normalize_notebook_target_rejects_non_colab_domain():
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    with pytest.raises(ValueError, match="Google Colab"):
        browser_manager.normalize_notebook_target(
            "https://example.com/drive/abcDEF_12345"
        )


def test_normalize_notebook_target_rejects_non_notebook_colab_path():
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    with pytest.raises(ValueError, match="notebook"):
        browser_manager.normalize_notebook_target(
            "https://colab.research.google.com/signup"
        )


@pytest.mark.asyncio
async def test_open_notebook_validates_target_before_starting_browser(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    starts = []

    async def starter():
        starts.append(True)
        raise AssertionError("browser must not start for an invalid target")

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path),
        playwright_starter=starter,
    )

    with pytest.raises(ValueError, match="Google Colab"):
        await manager.open_notebook("https://example.com/drive/abcDEF_12345")

    assert starts == []


def test_build_mcp_connection_url_preserves_query_and_replaces_fragment():
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    result = browser_manager.build_mcp_connection_url(
        "https://colab.research.google.com/drive/abcDEF_12345?usp=sharing#old",
        port=18585,
        token="secret-token",
    )

    assert result == (
        "https://colab.research.google.com/drive/abcDEF_12345"
        "?usp=sharing&p=18585"
        "#mcpProxyToken=secret-token&mcpProxyPort=18585"
    )


@pytest.mark.parametrize("port", [0, 65536])
def test_build_mcp_connection_url_rejects_invalid_port(port):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    with pytest.raises(ValueError, match="port"):
        browser_manager.build_mcp_connection_url(
            "abcDEF_12345",
            port=port,
            token="secret-token",
        )


def test_build_mcp_connection_url_rejects_empty_token():
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    with pytest.raises(ValueError, match="token"):
        browser_manager.build_mcp_connection_url(
            "abcDEF_12345",
            port=18585,
            token="   ",
        )


@pytest.mark.asyncio
async def test_manager_launches_persistent_context_with_dedicated_profile(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    calls = []

    class FakeContext:
        pages = []

        async def close(self):
            calls.append(("context.close",))

    class FakeChromium:
        async def launch_persistent_context(self, profile_dir, **kwargs):
            calls.append(("launch", profile_dir, kwargs))
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            calls.append(("playwright.stop",))

    async def starter():
        return FakePlaywright()

    profile_dir = tmp_path / "chrome-profile"
    config = browser_manager.BrowserConfig(
        profile_dir=profile_dir,
        executable_path=Path("/opt/google/chrome"),
        headless=True,
    )
    manager = browser_manager.ColabBrowserManager(
        config=config,
        playwright_starter=starter,
    )

    await manager.ensure_started()
    await manager.close()

    assert profile_dir.is_dir()
    assert calls[0] == (
        "launch",
        str(profile_dir),
        {
            "headless": True,
            "executable_path": "/opt/google/chrome",
        },
    )
    assert calls[-2:] == [("context.close",), ("playwright.stop",)]


@pytest.mark.asyncio
async def test_close_stops_playwright_and_clears_references_when_context_close_fails(
    tmp_path,
):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    events = []

    class FakeContext:
        async def close(self):
            events.append("context.close")
            raise RuntimeError("close failed")

    class FakePlaywright:
        async def stop(self):
            events.append("playwright.stop")

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()
    manager._playwright = FakePlaywright()

    with pytest.raises(RuntimeError, match="close failed"):
        await manager.close()

    assert events == ["context.close", "playwright.stop"]
    assert manager._context is None
    assert manager._playwright is None


@pytest.mark.asyncio
async def test_concurrent_start_launches_one_persistent_context(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    launch_count = 0

    class FakeContext:
        pages = []

    class FakeChromium:
        async def launch_persistent_context(self, profile_dir, **kwargs):
            nonlocal launch_count
            launch_count += 1
            await asyncio.sleep(0)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

    async def starter():
        await asyncio.sleep(0)
        return FakePlaywright()

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path),
        playwright_starter=starter,
    )

    first, second = await asyncio.gather(
        manager.ensure_started(),
        manager.ensure_started(),
    )

    assert first is second
    assert launch_count == 1


@pytest.mark.asyncio
async def test_externally_closed_context_is_relaunched(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    contexts = []

    class FakeContext:
        pages = []

        def on(self, event, callback):
            assert event == "close"
            self.close_callback = callback

    class FakeChromium:
        async def launch_persistent_context(self, profile_dir, **kwargs):
            context = FakeContext()
            contexts.append(context)
            return context

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            pass

    async def starter():
        return FakePlaywright()

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path),
        playwright_starter=starter,
    )
    first = await manager.ensure_started()

    first.close_callback()

    assert manager.status().running is False
    second = await manager.ensure_started()
    assert second is not first
    assert len(contexts) == 2


@pytest.mark.asyncio
async def test_failed_launch_stops_playwright_driver(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    events = []

    class FakeChromium:
        async def launch_persistent_context(self, profile_dir, **kwargs):
            raise RuntimeError("profile is locked")

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            events.append("playwright.stop")

    async def starter():
        return FakePlaywright()

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path),
        playwright_starter=starter,
    )

    with pytest.raises(browser_manager.BrowserLaunchError, match="profile"):
        await manager.ensure_started()

    assert events == ["playwright.stop"]
    assert manager.status().running is False


@pytest.mark.asyncio
async def test_failed_launch_clears_driver_even_when_driver_stop_fails(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    class FakeChromium:
        async def launch_persistent_context(self, profile_dir, **kwargs):
            raise RuntimeError("profile is locked")

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            raise RuntimeError("driver stop failed")

    async def starter():
        return FakePlaywright()

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path),
        playwright_starter=starter,
    )

    with pytest.raises(browser_manager.BrowserLaunchError, match="profile"):
        await manager.ensure_started()

    assert manager._playwright is None
    assert manager.status().running is False


@pytest.mark.asyncio
async def test_manager_uses_browser_channel_without_executable(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    launch_options = []

    class FakeContext:
        pages = []

    class FakeChromium:
        async def launch_persistent_context(self, profile_dir, **kwargs):
            launch_options.append(kwargs)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

    async def starter():
        return FakePlaywright()

    manager = browser_manager.ColabBrowserManager(
        config=browser_manager.BrowserConfig(
            profile_dir=tmp_path,
            channel="chrome",
        ),
        playwright_starter=starter,
    )

    await manager.ensure_started()

    assert launch_options == [{"headless": False, "channel": "chrome"}]


@pytest.mark.asyncio
async def test_open_notebook_reuses_matching_tab_and_redacts_token(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    drive_id = "11-SndejEz6MCYARW021CFjs5SCI9EXW"
    navigations = []

    class FakePage:
        url = f"https://colab.research.google.com/drive/{drive_id}#old"

        async def goto(self, url, **kwargs):
            self.url = url
            navigations.append((url, kwargs))

        async def bring_to_front(self):
            pass

        async def title(self):
            return "Turkish notebook"

    class FakeContext:
        pages = [FakePage()]

        async def new_page(self):
            raise AssertionError("matching page should be reused")

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    tab = await manager.open_notebook(
        drive_id,
        connection_port=18585,
        connection_token="do-not-leak",
    )

    assert len(navigations) == 1
    assert "mcpProxyToken=do-not-leak" in navigations[0][0]
    assert tab.title == "Turkish notebook"
    assert tab.selected is True
    assert "do-not-leak" not in tab.url
    assert "#" not in tab.url


@pytest.mark.asyncio
async def test_open_notebook_brings_selected_page_to_front(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    events = []

    class FakePage:
        url = "https://colab.research.google.com/drive/abcDEF_12345"

        async def goto(self, url, **kwargs):
            self.url = url

        async def title(self):
            return "Notebook"

        async def bring_to_front(self):
            events.append("bring_to_front")

    class FakeContext:
        pages = [FakePage()]

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    tab = await manager.open_notebook("abcDEF_12345")

    assert tab.selected is True
    assert events == ["bring_to_front"]


@pytest.mark.asyncio
async def test_concurrent_notebook_opens_are_serialized(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    new_page_count = 0

    class FakePage:
        url = "about:blank"

        async def goto(self, url, **kwargs):
            await asyncio.sleep(0)
            self.url = url

        async def bring_to_front(self):
            pass

        async def title(self):
            return "Notebook"

    class FakeContext:
        def __init__(self):
            self.pages = []

        async def new_page(self):
            nonlocal new_page_count
            new_page_count += 1
            await asyncio.sleep(0)
            page = FakePage()
            self.pages.append(page)
            return page

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    await asyncio.gather(
        manager.open_notebook("abcDEF_12345"),
        manager.open_notebook("abcDEF_12345"),
    )

    assert new_page_count == 1


@pytest.mark.asyncio
async def test_clear_connection_fragment_replaces_browser_history_url(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    evaluations = []

    class FakePage:
        url = (
            "https://colab.research.google.com/drive/abcDEF_12345?p=18585"
            "#mcpProxyToken=do-not-leak&mcpProxyPort=18585"
        )

        async def evaluate(self, expression, argument):
            evaluations.append((expression, argument))
            self.url = argument

    class FakeContext:
        pages = [FakePage()]

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    await manager.clear_connection_fragment("abcDEF_12345")

    assert evaluations == [
        (
            "url => history.replaceState(null, '', url)",
            "https://colab.research.google.com/drive/abcDEF_12345?p=18585",
        )
    ]


@pytest.mark.asyncio
async def test_clear_connection_fragment_fails_if_fragment_still_present_after_replace(
    tmp_path,
):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    class FakePage:
        url = (
            "https://colab.research.google.com/drive/abcDEF_12345?p=18585"
            "#mcpProxyToken=do-not-leak&mcpProxyPort=18585"
        )

        async def evaluate(self, expression, argument):
            # Simulate a browser/extension/CSP oddity where evaluate returns but
            # the current history entry still contains the sensitive fragment.
            assert expression == "url => history.replaceState(null, '', url)"
            assert "do-not-leak" not in argument

    class FakeContext:
        pages = [FakePage()]

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    with pytest.raises(browser_manager.BrowserNavigationError, match="verify"):
        await manager.clear_connection_fragment("abcDEF_12345")


@pytest.mark.asyncio
async def test_clear_connection_fragment_fails_if_target_tab_redirected(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    class FakePage:
        url = "https://accounts.google.com/signin"

    class FakeContext:
        pages = [FakePage()]

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    with pytest.raises(browser_manager.BrowserNavigationError, match="matching Colab"):
        await manager.clear_connection_fragment("abcDEF_12345")


@pytest.mark.asyncio
async def test_navigation_errors_do_not_expose_connection_token(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")
    closed = []

    class FakePage:
        url = "about:blank"

        async def goto(self, url, **kwargs):
            raise RuntimeError(f"navigation failed for {url}")

        async def close(self):
            closed.append(True)

    class FakeContext:
        pages = []

        async def new_page(self):
            return FakePage()

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    with pytest.raises(browser_manager.BrowserNavigationError) as error:
        await manager.open_notebook(
            "abcDEF_12345",
            connection_port=18585,
            connection_token="do-not-leak",
        )

    assert "do-not-leak" not in str(error.value)
    assert "mcpProxyToken" not in str(error.value)
    assert closed == [True]


@pytest.mark.asyncio
async def test_list_colab_tabs_excludes_other_sites_and_redacts_fragments(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    class FakePage:
        def __init__(self, url, title):
            self.url = url
            self._title = title

        async def title(self):
            return self._title

    class FakeContext:
        pages = [
            FakePage(
                "https://colab.research.google.com/drive/abcDEF_12345"
                "?p=18585#mcpProxyToken=secret&mcpProxyPort=18585",
                "Notebook A",
            ),
            FakePage("https://example.com", "Other"),
        ]

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(profile_dir=tmp_path)
    )
    manager._context = FakeContext()

    tabs = await manager.list_colab_tabs()

    assert tabs == [
        browser_manager.ColabTab(
            title="Notebook A",
            url=("https://colab.research.google.com/drive/abcDEF_12345?p=18585"),
            selected=False,
        )
    ]


def test_status_reports_running_context_without_sensitive_data(tmp_path):
    browser_manager = importlib.import_module("colab_mcp.browser_manager")

    class FakePage:
        def __init__(self, url):
            self.url = url

    class FakeContext:
        pages = [
            FakePage(
                "https://colab.research.google.com/drive/abcDEF_12345"
                "#mcpProxyToken=secret"
            ),
            FakePage("https://example.com"),
        ]

    manager = browser_manager.ColabBrowserManager(
        browser_manager.BrowserConfig(
            profile_dir=tmp_path,
            executable_path=Path("/opt/chrome"),
            headless=True,
        )
    )
    manager._context = FakeContext()

    status = manager.status()

    assert status == browser_manager.BrowserStatus(
        running=True,
        profile_dir=str(tmp_path),
        executable_path="/opt/chrome",
        headless=True,
        tab_count=2,
        colab_tab_count=1,
    )
    assert "secret" not in repr(status)
