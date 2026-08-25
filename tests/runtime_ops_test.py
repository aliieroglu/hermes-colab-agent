"""Tests for UI-driven runtime status and control.

These helpers read Colab's own DOM and send its keyboard shortcuts, so the
tests pin the behaviour that matters when Colab changes: never claim a state
that was not observed, and never swallow a delivery failure.
"""

import pytest

from colab_mcp import runtime_ops


class FakePage:
    def __init__(self, probe=None, *, evaluate_error=None, confirm_visible=False):
        self._probe = probe
        self._evaluate_error = evaluate_error
        self._confirm_visible = confirm_visible
        self.keys: list[str] = []
        self.brought_to_front = False
        self.clicked = False
        self.keyboard = self._Keyboard(self)

    class _Keyboard:
        def __init__(self, page):
            self._page = page

        async def press(self, key: str) -> None:
            self._page.keys.append(key)

    class _Locator:
        def __init__(self, page):
            self._page = page

        @property
        def first(self):
            return self

        async def is_visible(self) -> bool:
            return self._page._confirm_visible

        async def click(self) -> None:
            self._page.clicked = True

    async def bring_to_front(self) -> None:
        self.brought_to_front = True

    async def evaluate(self, script):
        if self._evaluate_error is not None:
            raise self._evaluate_error
        return self._probe

    def get_by_role(self, role, name=None):
        return self._Locator(self)


@pytest.mark.parametrize(
    "probe,expected",
    [
        ({"button": True, "attributes": {"connected": ""}}, "connected"),
        ({"button": True, "attributes": {"connecting": ""}}, "connecting"),
        ({"button": True, "label": "RAM Disk", "attributes": {}}, "connected"),
        ({"button": True, "label": "Connecting...", "attributes": {}}, "connecting"),
        ({"button": True, "label": "Reconnect", "attributes": {}}, "disconnected"),
        # Live evidence (2026-08-25): a *connected* A100 runtime rendered this
        # label, because the shadow root also carries hidden nodes. A bare
        # "Connect" therefore proves nothing and must not read as disconnected.
        (
            {"button": True, "label": "Connect A100High-RAM arrow_drop_down"},
            "unknown",
        ),
        ({"button": True, "label": "Connect", "attributes": {}}, "unknown"),
        ({"button": True, "ariaLabel": "Connected to Python 3"}, "connected"),
        ({"button": False}, "unknown"),
        ({"button": True, "label": "Baglan", "attributes": {}}, "unknown"),
        ("not a dict", "unknown"),
    ],
)
def test_classify_runtime_state(probe, expected):
    assert runtime_ops.classify_runtime_state(probe) == expected


@pytest.mark.asyncio
async def test_read_runtime_status_returns_state_and_raw_probe():
    page = FakePage({"button": True, "label": "RAM Disk", "attributes": {}})

    status = await runtime_ops.read_runtime_status(page)

    assert status["state"] == "connected"
    assert status["probe"]["label"] == "RAM Disk"
    assert "detail" not in status


@pytest.mark.asyncio
async def test_read_runtime_status_flags_an_uninterpretable_indicator():
    page = FakePage({"button": True, "label": "yukleniyor", "attributes": {}})

    status = await runtime_ops.read_runtime_status(page)

    assert status["state"] == "unknown"
    assert "unconfirmed" in status["detail"]
    assert status["probe"]["label"] == "yukleniyor"


@pytest.mark.asyncio
async def test_read_runtime_status_raises_a_token_safe_error():
    page = FakePage(
        evaluate_error=RuntimeError("https://colab.../#mcpProxyToken=s3cret")
    )

    with pytest.raises(runtime_ops.RuntimeControlError) as excinfo:
        await runtime_ops.read_runtime_status(page)

    assert "s3cret" not in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_interrupt_sends_the_colab_chord_to_the_focused_tab():
    page = FakePage()

    result = await runtime_ops.interrupt_runtime(page)

    assert page.brought_to_front is True
    assert page.keys == ["Control+m", "i"]
    assert result == {
        "action": "interrupt",
        "status": "requested",
        "detail": result["detail"],
    }
    assert "wait_for_cell" in result["detail"]


@pytest.mark.asyncio
async def test_restart_confirms_the_dialog_and_reports_runtime_state():
    page = FakePage(
        {"button": True, "label": "Reconnect", "attributes": {}}, confirm_visible=True
    )

    result = await runtime_ops.restart_runtime(page)

    assert page.keys == ["Control+m", "."]
    assert page.clicked is True
    assert result["dialog_confirmed"] is True
    assert result["runtime"]["state"] == "disconnected"


@pytest.mark.asyncio
async def test_restart_says_so_when_no_dialog_was_matched():
    page = FakePage({"button": True, "attributes": {"connected": ""}})

    result = await runtime_ops.restart_runtime(page)

    assert result["dialog_confirmed"] is False
    assert "accept it in the browser window" in result["detail"]


@pytest.mark.asyncio
async def test_control_actions_fail_when_the_shortcut_cannot_be_delivered():
    class DeadPage(FakePage):
        async def bring_to_front(self):
            raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(runtime_ops.RuntimeControlError, match="shortcut"):
        await runtime_ops.interrupt_runtime(DeadPage())


@pytest.mark.asyncio
async def test_probe_runtime_reports_hardware_from_the_runtime_itself(tmp_path):
    """The verdict must come from executed code, not from the connect button."""
    from fake_colab import FakeColab

    colab = FakeColab()

    result = await runtime_ops.probe_runtime(colab.call_tool)

    assert result["state"] == "connected"
    assert isinstance(result["runtime"]["python"], str)
    assert "cpu_count" in result["runtime"]
    assert "gpu" in result["runtime"]
    # The scratch cell it used is cleaned up.
    assert colab.cells == {}


@pytest.mark.asyncio
async def test_probe_runtime_calls_a_dead_runtime_disconnected():
    async def dead(name, arguments):
        raise RuntimeError("Not connected to a Colab browser session")

    result = await runtime_ops.probe_runtime(dead)

    assert result["state"] == "disconnected"
    assert "did not execute a probe snippet" in result["detail"]
