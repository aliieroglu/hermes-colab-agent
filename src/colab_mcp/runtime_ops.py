"""Runtime status and control driven through the managed Colab tab.

Colab exposes no local API for "is the kernel connected?", "interrupt", or
"restart" - the browser-side MCP surface only covers cells, and the OAuth
runtime API only covers accelerator assignment. These helpers therefore read
the notebook UI and send Colab's own keyboard shortcuts.

That makes them the most fragile part of this server: Colab can rename a DOM
node or a shortcut at any time. Every helper is written to degrade to
``unknown`` and return the raw probe result instead of asserting something it
could not confirm.
"""

from __future__ import annotations

import time
from typing import Any

# Colab keyboard shortcuts. Ctrl+M is the notebook command prefix on every
# platform, including macOS.
_COMMAND_PREFIX = "Control+m"
_INTERRUPT_KEY = "i"
_RESTART_KEY = "."

_CONFIRM_PATTERN = r"^\s*(yes|restart|ok|confirm)\s*$"

# Read every signal Colab currently exposes about the runtime, without
# assuming any single one of them exists.
_STATUS_PROBE = """() => {
  const probe = {button: false, label: null, ariaLabel: null,
                 attributes: {}, resources: null};
  const button = document.querySelector('colab-connect-button');
  if (!button) return probe;
  probe.button = true;
  for (const name of ['connected', 'connecting', 'busy', 'state', 'status']) {
    if (button.hasAttribute(name)) probe.attributes[name] = button.getAttribute(name) || '';
  }
  const aria = button.getAttribute('aria-label');
  if (aria) probe.ariaLabel = aria.trim();
  const root = button.shadowRoot;
  const host = root || button;
  const text = (host.textContent || '').replace(/\\s+/g, ' ').trim();
  if (text) probe.label = text.slice(0, 300);
  if (root) {
    const usage = root.querySelector('#usage, .usage, colab-resource-usage');
    if (usage) {
      const usageText = (usage.textContent || '').replace(/\\s+/g, ' ').trim();
      if (usageText) probe.resources = usageText.slice(0, 300);
    }
  }
  return probe;
}"""


RUNTIME_PROBE = """
import os, platform, shutil, subprocess, time
_gpu = None
try:
    _out = subprocess.run(
        ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'],
        capture_output=True, text=True, timeout=10)
    if _out.returncode == 0 and _out.stdout.strip():
        _gpu = _out.stdout.strip().splitlines()[0].strip()
except Exception:
    pass
try:
    _ram = round(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1e9, 1)
except Exception:
    _ram = None
try:
    _disk = round(shutil.disk_usage('/content').free / 1e9, 1)
except Exception:
    _disk = None
_result = {
    'python': platform.python_version(),
    'cpu_count': os.cpu_count(),
    'ram_total_gb': _ram,
    'disk_free_gb': _disk,
    'gpu': _gpu,
    'now': time.time(),
}
"""


class RuntimeControlError(RuntimeError):
    """A runtime control action could not be delivered to the notebook tab."""


def classify_runtime_state(probe: dict[str, Any]) -> str:
    """Map a raw UI probe onto ``connected`` / ``connecting`` /
    ``disconnected`` / ``unknown``.

    Only English Colab labels are recognised. Anything else falls through to
    ``unknown`` so the caller sees the raw label rather than a wrong verdict.
    """
    if not isinstance(probe, dict) or not probe.get("button"):
        return "unknown"

    attributes = probe.get("attributes")
    if isinstance(attributes, dict):
        if "connecting" in attributes:
            return "connecting"
        if "connected" in attributes:
            return "connected"

    haystack = " ".join(
        str(probe.get(key) or "") for key in ("label", "ariaLabel", "resources")
    ).lower()
    if not haystack.strip():
        return "unknown"
    if "connecting" in haystack or "initializing" in haystack:
        return "connecting"
    if "reconnect" in haystack or "disconnected" in haystack:
        return "disconnected"
    if "ram" in haystack and "disk" in haystack:
        return "connected"
    if "connected" in haystack:
        return "connected"
    # A live probe on 2026-08-25 returned "Connect A100High-RAM arrow_drop_down"
    # from a *connected* runtime: the shadow root concatenates hidden nodes, so
    # the word "connect" is not evidence of anything. Only the visible usage
    # gauge is, and when that is absent the honest answer is unknown - the
    # semantic probe below is what actually decides.
    return "unknown"


async def read_runtime_status(page) -> dict:
    """Return the runtime state as read from the notebook UI."""
    try:
        probe = await page.evaluate(_STATUS_PROBE)
    except Exception as exc:
        raise RuntimeControlError(
            f"Could not read the Colab runtime indicator: {type(exc).__name__}"
        ) from None
    if not isinstance(probe, dict):
        probe = {"button": False, "raw": str(probe)}
    state = classify_runtime_state(probe)
    result = {"state": state, "probe": probe}
    if state == "unknown":
        result["detail"] = (
            "The Colab connect indicator did not expose a state this server "
            "recognises. The raw probe is included so you can decide; treat the "
            "runtime state as unconfirmed."
        )
    return result


async def _send_command(page, key: str) -> None:
    try:
        await page.bring_to_front()
        await page.keyboard.press(_COMMAND_PREFIX)
        await page.keyboard.press(key)
    except Exception as exc:
        raise RuntimeControlError(
            f"Could not deliver the Colab shortcut to the notebook tab: "
            f"{type(exc).__name__}"
        ) from None


async def _confirm_dialog(page) -> bool:
    """Click Colab's confirmation button if a dialog is showing."""
    try:
        button = page.get_by_role("button", name=_CONFIRM_PATTERN)
        if hasattr(button, "first"):
            button = button.first
        if not await button.is_visible():
            return False
        await button.click()
        return True
    except Exception:
        # No dialog, or a shape this selector does not match. The caller
        # reports the action as unconfirmed rather than failing.
        return False


async def interrupt_runtime(page) -> dict:
    """Send Colab's interrupt-execution shortcut (Ctrl+M I)."""
    await _send_command(page, _INTERRUPT_KEY)
    return {
        "action": "interrupt",
        "status": "requested",
        "detail": (
            "Interrupt was sent as a Colab keyboard shortcut. Confirm the effect "
            "with get_cells or wait_for_cell - the UI reports no acknowledgement "
            "this server can read."
        ),
    }


async def restart_runtime(page) -> dict:
    """Send Colab's restart-runtime shortcut (Ctrl+M .) and confirm the dialog."""
    await _send_command(page, _RESTART_KEY)
    confirmed = await _confirm_dialog(page)
    status = await read_runtime_status(page)
    return {
        "action": "restart",
        "status": "requested",
        "dialog_confirmed": confirmed,
        "runtime": status,
        "detail": (
            "Restart was sent as a Colab keyboard shortcut."
            + (
                ""
                if confirmed
                else " No confirmation dialog was matched; if Colab is waiting on "
                "one, accept it in the browser window."
            )
            + " All runtime state (variables, installed packages, /content files) "
            "is lost on restart."
        ),
    }


async def probe_runtime(call_tool) -> dict:
    """Ask the runtime itself whether it is alive, and what hardware it has.

    The connect indicator turned out to be unreadable in practice (see
    :func:`classify_runtime_state`), while executing a snippet is decisive: a
    runtime that answers is connected, and the answer carries the accelerator
    and memory the DOM never reliably exposed.
    """
    from colab_mcp import notebook_ops

    started = time.monotonic()
    try:
        details = await notebook_ops.run_python_json(call_tool, RUNTIME_PROBE)
    except Exception as exc:
        return {
            "state": "disconnected",
            "detail": (
                "The runtime did not execute a probe snippet, so it is not "
                f"usable right now: {type(exc).__name__}: {exc}"
            ),
        }
    return {
        "state": "connected",
        "elapsed_sec": round(time.monotonic() - started, 2),
        "runtime": details,
    }
