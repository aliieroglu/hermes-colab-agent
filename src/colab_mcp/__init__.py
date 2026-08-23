# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import asyncio
from dataclasses import asdict
import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
import sys


from fastmcp import FastMCP
from fastmcp.utilities import logging as fastmcp_logger

from colab_mcp.session import ColabSessionProxy, NOT_CONNECTED_MSG
from colab_mcp.websocket_server import COLAB, SCRATCH_PATH
from colab_mcp.browser_manager import BrowserConfig, ColabBrowserManager
from colab_mcp import jobs, notebook_ops, process_registry, runtime_ops


mcp = FastMCP(name="ColabMCP")

# These will be set during main_async() startup
_proxy_client = None
_session_mcp = None
_colab_client = None  # For runtime API (assign/unassign GPU)
_browser_manager: ColabBrowserManager | None = None
# Notebook most recently opened through this server. Runtime tools default to
# it so an agent does not have to repeat the target on every call.
_current_target: str | None = None


def _resolve_target(notebook_url_or_drive_id: str = "") -> str:
    """Pick the notebook a tool should act on."""
    return notebook_url_or_drive_id or _current_target or f"{COLAB}{SCRATCH_PATH}"


async def _proxy_call(tool_name: str, arguments: dict) -> str:
    """Call a browser-side tool, raising instead of returning a stub message."""
    if _proxy_client is None or not _proxy_client.is_connected():
        raise notebook_ops.NotebookOpsError(NOT_CONNECTED_MSG)
    result = await _proxy_client.proxy_mcp_client.call_tool(tool_name, arguments)
    if hasattr(result, "content"):
        return "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return str(result)


async def _notebook_page(purpose: str, notebook_url_or_drive_id: str = ""):
    """Return the managed page for a notebook, for UI-level control."""
    if _browser_manager is None:
        raise runtime_ops.RuntimeControlError(
            "Playwright browser management is disabled, so the notebook UI "
            "cannot be controlled."
        )
    return await _browser_manager.notebook_page(
        _resolve_target(notebook_url_or_drive_id), purpose=purpose
    )


def _control_error(tool_name: str, exc: Exception) -> str:
    """Render a control-tool failure without leaking URLs or tokens."""
    if isinstance(exc, runtime_ops.RuntimeControlError):
        return str(exc)
    from colab_mcp.browser_manager import BrowserNavigationError

    if isinstance(exc, BrowserNavigationError):
        return (
            f"{exc}. Open the notebook with open_colab_notebook first, or pass "
            "its Drive ID explicitly."
        )
    logging.exception("%s failed", tool_name)
    return f"{tool_name} failed: {type(exc).__name__}. See the server log for details."


async def _forward_or_stub(tool_name: str, arguments: dict) -> str:
    """Forward a tool call to the browser if connected, otherwise return stub message."""
    if _proxy_client is not None and _proxy_client.is_connected():
        try:
            result = await _proxy_client.proxy_mcp_client.call_tool(
                tool_name, arguments
            )
            # Extract text from result
            if hasattr(result, "content"):
                return "\n".join(c.text for c in result.content if hasattr(c, "text"))
            return str(result)
        except Exception as e:
            return f"Error calling {tool_name}: {e}. Try calling open_colab_browser_connection to reconnect."
    return NOT_CONNECTED_MSG


@mcp.tool()
async def get_colab_browser_status() -> str:
    """Return a redacted status summary for the managed Playwright browser."""
    if _browser_manager is None:
        return json.dumps({"enabled": False, "running": False})
    return json.dumps({"enabled": True, **asdict(_browser_manager.status())})


@mcp.tool()
async def list_colab_tabs() -> str:
    """List open Colab tabs without exposing MCP connection tokens."""
    if _browser_manager is None:
        return json.dumps([])
    tabs = await _browser_manager.list_colab_tabs()
    return json.dumps([asdict(tab) for tab in tabs])


@mcp.tool()
async def open_colab_notebook(notebook_url_or_drive_id: str) -> str:
    """Open or reuse a trusted Colab notebook in the managed browser."""
    global _current_target
    if _browser_manager is None:
        return "Playwright browser management is disabled."
    tab = await _browser_manager.open_notebook(notebook_url_or_drive_id)
    _current_target = notebook_url_or_drive_id
    return json.dumps(asdict(tab))


async def _connect_to_notebook(target: str) -> str:
    """Attach the browser-side MCP proxy to ``target`` and verify it works."""
    global _current_target

    if _proxy_client is None:
        return "Server not initialized. Please wait and try again."

    if _browser_manager is None:
        return (
            "Playwright browser management is disabled; refusing to place the "
            "MCP connection token in a system-browser command-line URL."
        )
    await _browser_manager.open_notebook(
        target,
        connection_port=_proxy_client.wss.port,
        connection_token=_proxy_client.wss.token,
    )
    _current_target = target

    # Wait for Colab to consume the fragment before removing it from the
    # current browser history entry. Also attempt removal after a timeout or
    # connection error so the token does not remain in session history.
    fragment_cleared = True
    try:
        await _proxy_client.await_proxy_connection()
    finally:
        try:
            await _browser_manager.clear_connection_fragment(target)
        except Exception:
            fragment_cleared = False
            logging.warning("Could not clear browser connection fragment")

    if not fragment_cleared:
        return (
            "Connection attempt stopped because the MCP token fragment could not be "
            "securely cleared from browser history. Close the Colab tab and retry."
        )

    if _proxy_client.is_connected():
        tool_names = await _proxy_client.await_tools_ready()
        if "get_cells" not in tool_names:
            return (
                "Connection established, but semantic verification failed: "
                "the browser did not expose get_cells. Retry the connection."
            )
        proxy_mcp_client = _proxy_client.proxy_mcp_client
        if proxy_mcp_client is None:
            return (
                "Connection established, but semantic verification failed: "
                "the browser MCP client is not ready. Retry the connection."
            )
        try:
            await proxy_mcp_client.call_tool("get_cells", {})
        except Exception:
            return (
                "Connection established, but the get_cells semantic probe failed. "
                "Retry the connection."
            )
        tools_text = ", ".join(tool_names) if tool_names else "none discovered"
        return f"Connection successful. Available notebook tools: {tools_text}. You can now create, edit, and execute cells in the Colab notebook."

    # Timed out — surface diagnostic info about other running servers so the
    # user can recognize the "old browser tab pointed at a dead port" case.
    try:
        others = [e for e in process_registry.list_running() if e.pid != os.getpid()]
    except Exception:
        others = []
    my_port = _proxy_client.wss.port
    if others:
        peer_ports = ", ".join(f"{e.port} (pid {e.pid})" for e in others)
        return (
            f"Connection timed out. This server is on port {my_port}, but "
            f"{len(others)} other colab-mcp server(s) are also running: "
            f"{peer_ports}. If you have an old Colab tab open, it may be "
            "pointing at one of those instead of this server. Either close "
            "the old tab and let me open a fresh one, or run `colab-mcp "
            "--kill-stale` to clean up orphaned servers."
        )
    return (
        f"Connection timed out. This server is on port {my_port}. Common causes:\n"
        "  1. Stale Colab tab(s) - if Chrome reused an old tab whose URL "
        "fragment points at a dead port, the tab will say 'Disconnected from "
        "the local Colab MCP server'. Close every existing colab.research.google.com "
        "tab, then retry - `?p=<port>` in the URL is used to force Chrome to "
        "open a fresh tab per server instance.\n"
        "  2. Local Network Access permission denied - Chrome shows a prompt "
        "the first time Colab tries to reach localhost. Click 'Allow'. If you "
        "previously clicked 'Block', open colab.research.google.com -> site "
        "settings -> reset the 'Insecure content' / 'Other' permission and retry.\n"
        "  3. Browser tab was never opened - make sure your default browser "
        "is set and not blocking pop-ups for python.exe."
    )


@mcp.tool()
async def open_colab_browser_connection(notebook_url_or_drive_id: str = "") -> str:
    """Open a Colab notebook and attach its browser-side MCP proxy.

    When omitted, ``notebook_url_or_drive_id`` opens Colab's scratchpad. A
    Drive ID or trusted Colab URL selects an existing notebook tab when
    possible. The connection token is passed only to the browser manager and
    is never returned by this tool.
    """
    if _proxy_client is not None and _proxy_client.is_connected():
        return "Already connected to Colab."
    return await _connect_to_notebook(
        notebook_url_or_drive_id or f"{COLAB}{SCRATCH_PATH}"
    )


@mcp.tool()
async def reconnect_notebook(notebook_url_or_drive_id: str = "") -> str:
    """Re-attach the browser-side MCP proxy after the Colab tab dropped.

    Unlike open_colab_browser_connection this does not short-circuit when the
    server still believes it is connected: it re-runs the full connect and
    verification flow. Use it when notebook tools start failing mid-session,
    which usually means the tab reloaded, the runtime was recycled, or the
    laptop slept.
    """
    target = _resolve_target(notebook_url_or_drive_id)
    return await _connect_to_notebook(target)


@mcp.tool()
async def add_code_cell(
    code: str = "", cellIndex: int = 0, language: str = "python"
) -> str:
    """Add a new code cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub(
        "add_code_cell", {"code": code, "cellIndex": cellIndex, "language": language}
    )


@mcp.tool()
async def add_text_cell(content: str = "", cellIndex: int = -1) -> str:
    """Add a new text/markdown cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub(
        "add_text_cell", {"content": content, "cellIndex": cellIndex}
    )


@mcp.tool()
async def get_cells() -> str:
    """Read the current notebook state: list of cells with their IDs, contents, and outputs. Essential for iterative work (write -> run -> read -> adjust). Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("get_cells", {})


@mcp.tool()
async def run_code_cell(cellId: str = "") -> str:
    """Execute a code cell in the Colab notebook by cellId (from add_code_cell or get_cells). Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("run_code_cell", {"cellId": cellId})


@mcp.tool()
async def update_cell(cellId: str = "", content: str = "") -> str:
    """Update the contents of an existing cell in the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("update_cell", {"cellId": cellId, "content": content})


@mcp.tool()
async def delete_cell(cellId: str = "") -> str:
    """Delete a cell from the Colab notebook by cellId. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("delete_cell", {"cellId": cellId})


@mcp.tool()
async def move_cell(cellId: str = "", cellIndex: int = 0) -> str:
    """Move a cell to a new position in the Colab notebook by cellId and target index. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub(
        "move_cell", {"cellId": cellId, "cellIndex": cellIndex}
    )


@mcp.tool()
async def wait_for_cell(
    cellId: str = "",
    timeout_sec: float = 600.0,
    poll_interval_sec: float = 3.0,
) -> str:
    """Wait for a running cell to finish, polling the notebook instead of blocking.

    run_code_cell returns only once Colab finishes executing, which is unusable
    for long jobs: the MCP call blocks with no progress and clients time out.
    Start such work in the notebook, then poll here. Returns JSON with status
    completed / running / timeout / missing / unknown - a timeout means the wait
    expired, not that the cell stopped, so poll again or call interrupt_runtime.
    Requires an active browser connection.
    """
    try:
        result = await notebook_ops.wait_for_cell(
            _proxy_call,
            cellId,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
    except notebook_ops.NotebookOpsError as exc:
        return str(exc)
    return json.dumps(result)


@mcp.tool()
async def get_runtime_status(notebook_url_or_drive_id: str = "") -> str:
    """Report whether the Colab runtime is connected, plus RAM/disk if shown.

    Read from the notebook UI, because Colab exposes no local API for it.
    Returns JSON with state connected / connecting / disconnected / unknown and
    the raw probe; unknown means the indicator could not be interpreted, not
    that the runtime is down. Defaults to the notebook most recently opened by
    this server.
    """
    try:
        page = await _notebook_page("read runtime status", notebook_url_or_drive_id)
        return json.dumps(await runtime_ops.read_runtime_status(page))
    except Exception as exc:
        return _control_error("get_runtime_status", exc)


@mcp.tool()
async def interrupt_runtime(notebook_url_or_drive_id: str = "") -> str:
    """Interrupt the cell currently executing, keeping variables and files.

    Experimental: delivered as Colab's own keyboard shortcut to the managed
    tab, so it depends on Colab's UI. Confirm the effect with get_cells or
    wait_for_cell.
    """
    try:
        page = await _notebook_page("interrupt the runtime", notebook_url_or_drive_id)
        return json.dumps(await runtime_ops.interrupt_runtime(page))
    except Exception as exc:
        return _control_error("interrupt_runtime", exc)


@mcp.tool()
async def restart_runtime(notebook_url_or_drive_id: str = "") -> str:
    """Restart the Colab runtime, discarding all in-memory and /content state.

    Experimental: delivered as Colab's own keyboard shortcut to the managed
    tab, and any confirmation dialog is accepted automatically. Everything the
    session held - variables, pip installs, files outside Drive - is lost.
    """
    try:
        page = await _notebook_page("restart the runtime", notebook_url_or_drive_id)
        return json.dumps(await runtime_ops.restart_runtime(page))
    except Exception as exc:
        return _control_error("restart_runtime", exc)


@mcp.tool()
async def upload_file(
    local_path: str = "",
    remote_path: str = "",
    chunk_bytes: int = notebook_ops.DEFAULT_CHUNK_BYTES,
    max_bytes: int = notebook_ops.MAX_TRANSFER_BYTES,
) -> str:
    """Copy a local file into the Colab runtime, verified with SHA-256.

    The bytes travel as base64 through a temporary code cell that is removed
    afterwards. Intended for datasets, configs, and checkpoints up to a few
    tens of MB; stage anything larger through Drive. Requires an active
    browser connection.
    """
    try:
        result = await notebook_ops.upload_file(
            _proxy_call,
            local_path,
            remote_path,
            chunk_bytes=chunk_bytes,
            max_bytes=max_bytes,
        )
    except notebook_ops.NotebookOpsError as exc:
        return str(exc)
    return json.dumps(result)


@mcp.tool()
async def download_file(
    remote_path: str = "",
    local_path: str = "",
    chunk_bytes: int = notebook_ops.DEFAULT_CHUNK_BYTES,
    max_bytes: int = notebook_ops.MAX_TRANSFER_BYTES,
    overwrite: bool = False,
) -> str:
    """Copy a file out of the Colab runtime to this machine, verified with SHA-256.

    Reads the file back as base64 through a temporary code cell that is removed
    afterwards. Use it to rescue checkpoints and logs before a runtime is
    recycled. Refuses to replace an existing local file unless overwrite=True.
    Requires an active browser connection.
    """
    try:
        result = await notebook_ops.download_file(
            _proxy_call,
            remote_path,
            local_path,
            chunk_bytes=chunk_bytes,
            max_bytes=max_bytes,
            overwrite=overwrite,
        )
    except notebook_ops.NotebookOpsError as exc:
        return str(exc)
    return json.dumps(result)


@mcp.tool()
async def create_job(
    job_id: str = "",
    stages_json: str = "",
    persist_dir: str = "",
    notebook_url_or_drive_id: str = "",
    work_dir: str = "/content",
) -> str:
    """Define a durable, resumable multi-stage job.

    stages_json is a JSON array of objects: {"id", "code", "artifacts": [paths],
    "timeout_sec"}. Each stage runs as a detached process inside the runtime, so
    it survives the browser tab closing, and a stage that finished with the same
    code is skipped when the job runs again. Declare the files a stage produces
    as artifacts: they are checksummed, and a stage that does not produce them
    counts as failed. persist_dir (for example a mounted Drive folder) receives
    a verified copy of every artifact.
    """
    try:
        manifest = jobs.create_job(
            job_id,
            stages_json,
            persist_dir=persist_dir,
            notebook=notebook_url_or_drive_id or None,
            work_dir=work_dir,
        )
    except jobs.JobError as exc:
        return str(exc)
    return json.dumps(jobs.describe_job(manifest))


@mcp.tool()
async def run_job(
    job_id: str = "",
    wait_sec: float = 120.0,
    poll_interval_sec: float = 10.0,
) -> str:
    """Run a job forward, skipping stages that already completed.

    Returns when the job finishes, a stage fails, or wait_sec expires - never
    open-ended. A "running" result means the stage is still going inside Colab,
    which continues whether or not this session is alive; call run_job again to
    keep following it. Requires an active browser connection.
    """
    try:
        manifest = jobs.load_manifest(job_id)
        result = await jobs.advance_job(
            _proxy_call,
            manifest,
            wait_sec=wait_sec,
            poll_interval_sec=poll_interval_sec,
        )
    except (jobs.JobError, notebook_ops.NotebookOpsError) as exc:
        return str(exc)
    return json.dumps(result)


@mcp.tool()
async def get_job_status(job_id: str = "", log_tail_chars: int = 1500) -> str:
    """Report per-stage state, log tails, and recent events for a job.

    Read-only: it starts nothing. Use it after reconnecting to find out what a
    job did while this server was away. A stage reported as stalled has stopped
    updating its heartbeat, which usually means the runtime was restarted.
    Requires an active browser connection.
    """
    try:
        manifest = jobs.load_manifest(job_id)
        result = await jobs.job_status(
            _proxy_call, manifest, log_tail_chars=log_tail_chars
        )
    except (jobs.JobError, notebook_ops.NotebookOpsError) as exc:
        return str(exc)
    return json.dumps(result)


@mcp.tool()
async def list_jobs() -> str:
    """List the jobs defined on this machine, with their stages and checksums."""
    summaries = []
    for job_id in jobs.list_job_ids():
        try:
            summaries.append(jobs.describe_job(jobs.load_manifest(job_id)))
        except jobs.JobError as exc:
            summaries.append({"job_id": job_id, "error": str(exc)})
    return json.dumps(summaries)


@mcp.tool()
async def reset_job_stage(job_id: str = "", stage_id: str = "") -> str:
    """Delete a stage's completion marker, status, and log so it runs again.

    Use it when a stage completed but produced something wrong; run_job would
    otherwise skip it. Requires an active browser connection.
    """
    try:
        manifest = jobs.load_manifest(job_id)
        result = await jobs.reset_stage(_proxy_call, manifest, stage_id)
    except (jobs.JobError, notebook_ops.NotebookOpsError) as exc:
        return str(exc)
    return json.dumps(result)


@mcp.tool()
async def change_runtime(accelerator: str = "T4") -> str:
    """Change the Colab runtime to use a specific GPU accelerator. Valid values: NONE, T4, L4, A100. Requires OAuth setup (first time opens browser for consent)."""
    if _colab_client is None:
        return "Runtime API not initialized. Start with --client-oauth-config flag pointing to your OAuth client secrets JSON."
    try:
        from colab_mcp.client import Accelerator, Variant
        import uuid

        acc = Accelerator(accelerator)
        variant = Variant.GPU if acc != Accelerator.NONE else Variant.DEFAULT
        notebook_hash = str(uuid.uuid4())

        # Unassign current VM if any
        try:
            assignments = _colab_client.list_assignments()
            for a in assignments:
                _colab_client.unassign(a.endpoint)
        except Exception:
            pass

        # Assign new VM
        result = _colab_client.assign(notebook_hash, variant, acc)
        return f"Runtime changed to {accelerator}. Endpoint: {result.endpoint}. Use open_colab_browser_connection to connect to the new runtime."
    except Exception as e:
        return f"Failed to change runtime: {e}"


def init_logger(logdir):
    log_filename = datetime.datetime.now().strftime(
        f"{logdir}/colab-mcp.%Y-%m-%d_%H-%M-%S.log"
    )
    logging.basicConfig(
        format="%(asctime)s %(levelname)s:%(message)s",
        datefmt="%m/%d/%Y %I:%M:%S %p",
        filename=log_filename,
        level=logging.INFO,
    )
    fastmcp_logger.get_logger("colab-mcp").info("logging to %s" % log_filename)


def parse_args(v):
    parser = argparse.ArgumentParser(
        description="ColabMCP is an MCP server that lets you interact with Colab."
    )
    parser.add_argument(
        "-l",
        "--log",
        help="if set, use this directory as a location for logfiles (if unset, will log to %s/colab-mcp-logs/)"
        % tempfile.gettempdir(),
        action="store",
        default=tempfile.mkdtemp(prefix="colab-mcp-logs-"),
    )
    parser.add_argument(
        "-p",
        "--enable-proxy",
        help="if set, enable the runtime proxy (enabled by default).",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--client-oauth-config",
        help="Path to OAuth client secrets JSON for Colab API access (enables change_runtime tool).",
        action="store",
        default=None,
    )
    parser.add_argument(
        "--no-playwright-browser",
        dest="playwright_browser",
        help="Disable Playwright browser lifecycle management; secure MCP browser connection opening will be unavailable.",
        action="store_false",
        default=True,
    )
    parser.add_argument(
        "--browser-profile-dir",
        help="Dedicated persistent Chrome profile used only by Hermes Colab Agent.",
        default=str(Path.home() / ".local/share/hermes-colab-agent/chrome-profile"),
    )
    parser.add_argument(
        "--browser-channel",
        help="Playwright browser channel. Defaults to installed stable Chrome.",
        default="chrome",
    )
    parser.add_argument(
        "--browser-executable",
        help="Optional explicit Chrome/Chromium executable path; overrides --browser-channel.",
        default=None,
    )
    parser.add_argument(
        "--browser-headless",
        help="Run the managed browser headlessly after initial login has been completed.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--browser-cdp-endpoint",
        help=(
            "Attach to an existing loopback-only Chrome DevTools endpoint "
            "instead of launching another browser (for example, "
            "http://127.0.0.1:9222)."
        ),
        default=None,
    )
    parser.add_argument(
        "--list-running",
        help="List all currently-running colab-mcp servers and exit.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--kill-stale",
        help="Terminate all running colab-mcp servers (including this one is NOT included) and exit. Useful when the browser shows 'Disconnected from the local Colab MCP server' due to orphaned processes from prior sessions.",
        action="store_true",
        default=False,
    )
    return parser.parse_args(v)


def create_browser_manager(args) -> ColabBrowserManager | None:
    """Create the lazily-started Playwright manager from CLI arguments."""
    if not args.playwright_browser:
        return None
    executable_path = (
        Path(args.browser_executable).expanduser() if args.browser_executable else None
    )
    return ColabBrowserManager(
        BrowserConfig(
            profile_dir=Path(args.browser_profile_dir).expanduser(),
            executable_path=executable_path,
            headless=args.browser_headless,
            channel=args.browser_channel or None,
            cdp_endpoint=args.browser_cdp_endpoint,
        )
    )


def _print_running_servers() -> None:
    entries = process_registry.list_running()
    if not entries:
        print("No colab-mcp servers currently registered as running.")
        return
    print(f"Found {len(entries)} running colab-mcp server(s):")
    import datetime as _dt

    for e in entries:
        started = _dt.datetime.fromtimestamp(e.started_at).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  pid={e.pid:<6}  port={e.port:<6}  host={e.host}  started={started}")


async def main_async():
    global _proxy_client, _session_mcp, _colab_client, _browser_manager
    args = parse_args(sys.argv[1:])
    init_logger(args.log)

    # Diagnostic / cleanup flags exit early.
    if args.list_running:
        _print_running_servers()
        return
    if args.kill_stale:
        removed = process_registry.cleanup_stale(kill=True)
        if not removed:
            print("No stale colab-mcp servers found.")
        else:
            print(f"Terminated {len(removed)} stale colab-mcp server(s):")
            for e in removed:
                print(f"  pid={e.pid} port={e.port}")
        return

    # Prune any dead entries from prior crashed runs BEFORE we bind a port.
    # This keeps the registry honest. We don't auto-kill ALIVE entries here —
    # multiple clients (e.g., two Claude Code instances) are valid; only the
    # browser-tab confusion is the bug, and the per-tab token fragment scopes
    # which server a tab talks to.
    dead = process_registry.prune_dead()
    if dead:
        logging.info(f"Pruned {dead} stale entries from process registry")

    _browser_manager = create_browser_manager(args)

    if args.enable_proxy:
        logging.info("enabling session proxy tools")
        _session_mcp = ColabSessionProxy()
        await _session_mcp.start_proxy_server()
        _proxy_client = _session_mcp.proxy_client
        # Register ourselves now that we know the port.
        try:
            entry = process_registry.register(
                port=_session_mcp.wss.port,
                host=_session_mcp.wss.host,
            )
            logging.info(f"Registered colab-mcp pid={entry.pid} port={entry.port}")
        except Exception as exc:
            logging.warning(f"Could not register process: {exc}")

    if args.client_oauth_config:
        try:
            from colab_mcp.auth import get_credentials
            from colab_mcp.client import ColabClient, Prod

            logging.info("initializing Colab API client with OAuth")
            session = get_credentials(args.client_oauth_config)
            _colab_client = ColabClient(Prod(), session)
            logging.info("Colab API client ready")
        except Exception as e:
            logging.warning(f"Failed to initialize Colab API client: {e}")

    try:
        await mcp.run_async()

    finally:
        try:
            if args.enable_proxy and _session_mcp:
                await _session_mcp.cleanup()
        finally:
            try:
                if _browser_manager is not None:
                    browser_manager, _browser_manager = _browser_manager, None
                    await browser_manager.close()
            finally:
                # Always unregister so a clean shutdown doesn't leave a stale entry.
                try:
                    process_registry.unregister()
                except Exception as exc:
                    logging.warning(f"Could not unregister process: {exc}")


def main() -> None:
    asyncio.run(main_async())
