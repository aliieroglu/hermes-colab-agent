# Hermes Colab Agent

[![CI](https://github.com/aliieroglu/hermes-colab-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/aliieroglu/hermes-colab-agent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple.svg)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/aliieroglu/hermes-colab-agent?style=social)](https://github.com/aliieroglu/hermes-colab-agent)

An MCP server for reliable Google Colab control from Hermes and other AI coding agents. It combines semantic notebook tools with a Playwright-managed persistent Chrome profile, avoiding fragile screen-coordinate automation while keeping Google login and 2FA under the user's control.

## Project Lineage and Hermes Roadmap

This public fork builds on [Google's original `colab-mcp`](https://github.com/googlecolab/colab-mcp) and [SebastianGilPinzon's enhanced fork](https://github.com/SebastianGilPinzon/colab-mcp), preserving their Apache-2.0 license and Git history. Phase 1 adds a Playwright browser manager that owns a dedicated persistent profile, reuses notebook tabs by Drive ID, removes the MCP connection fragment from browser history after the proxy attempt consumes it, and exposes redacted browser status tools. Runtime/job/file durability remains future work.

## Why This Fork?

Four concrete problems that the official `googlecolab/colab-mcp` doesn't solve — and that this fork addresses:

1. **Invisible tools** ([#54](https://github.com/googlecolab/colab-mcp/discussions/54), [#67](https://github.com/googlecolab/colab-mcp/discussions/67), [#69](https://github.com/googlecolab/colab-mcp/discussions/69)) — only `open_colab_browser_connection` appears in most MCP clients (Claude Code, Codex, Kiro IDE). The notebook tools rely on `notifications/tools/list_changed`, which these clients ignore. Without `get_cells` in particular, the bridge is effectively write-only: an agent can add cells but can't read state back.
2. **"Disconnected from the local Colab MCP server"** ([#84](https://github.com/googlecolab/colab-mcp/discussions/84)) — orphaned servers from prior Claude Code sessions hold ports that your browser tab still points at. Reconnecting from the tab silently fails.
3. **No deterministic browser bootstrap** — system-browser calls can target the wrong profile or stale tab. Playwright now owns a dedicated persistent profile and selects notebooks semantically by URL/Drive ID.
4. **No programmatic GPU control** — Google [removed](https://github.com/googlecolab/colab-mcp/discussions/41) the `--enable-runtime` feature entirely. You can't assign T4 / L4 / A100 without clicking in the browser.

This fork pre-registers all 24 tools, manages a dedicated Chrome profile, redacts connection fragments from status output, detects stale servers, and retains the optional OAuth runtime client.

## What's Different

| Feature | Official | This Fork |
|---------|----------|-----------|
| Notebook tools visible at startup | No (needs browser + list_changed) | Yes (pre-registered, works with any client) |
| Persistent browser profile | No | Yes, isolated Playwright `launch_persistent_context` profile |
| Notebook selection | Scratchpad/system-browser only | Drive ID or trusted Colab URL; matching tabs are reused |
| Browser diagnostics | None | Redacted status and Colab-tab listing tools |
| `change_runtime` tool (GPU control) | Removed | Experimental via OAuth |
| OAuth token caching | N/A | Yes (authorize once, cached forever) |
| Windows compatibility | Port 53919 blocked | Fixed (port 8085) |
| ColabClient initialization | N/A | Fixed (Prod() env argument) |
| Stale-server detection / cleanup | None — silent "Disconnected" | `--list-running` + `--kill-stale`, registry pruning on startup |

## Available Tools

| Tool | Requires Browser | Requires OAuth | Description |
|------|:---:|:---:|-------------|
| `change_runtime` | | Yes | Experimental GPU assignment: T4, L4, A100, or NONE |
| `get_colab_browser_status` | | | Return managed-browser state without launching it or exposing fragments |
| `list_colab_tabs` | Yes | | List open Colab tabs with fragments removed |
| `open_colab_notebook` | Yes | | Open/reuse a notebook by Drive ID or trusted Colab URL |
| `open_colab_browser_connection` | Yes | | Open/reuse a notebook and apply the ephemeral MCP connection fragment |
| `add_code_cell` | Yes | | Add a code cell to the notebook |
| `add_text_cell` | Yes | | Add a markdown cell |
| `get_cells` | Yes | | Read current notebook state (cells, IDs, contents, outputs) |
| `run_code_cell` | Yes | | Execute a code cell by `cellId` |
| `update_cell` | Yes | | Edit an existing cell by `cellId` |
| `delete_cell` | Yes | | Delete a cell by `cellId` |
| `move_cell` | Yes | | Move a cell to a new position by `cellId` |
| `wait_for_cell` | Yes | | Poll a running cell until it finishes, instead of blocking the MCP call |
| `reconnect_notebook` | Yes | | Re-run the full connect + verification flow after the tab dropped |
| `get_runtime_status` | Yes | | Run a probe snippet in the runtime: liveness plus GPU / RAM / free disk |
| `interrupt_runtime` | Yes | | Experimental: interrupt the executing cell, keeping runtime state |
| `restart_runtime` | Yes | | Experimental: restart the runtime, discarding all state |
| `upload_file` | Yes | | Copy a local file into the runtime, verified with SHA-256 |
| `download_file` | Yes | | Copy a file out of the runtime to this machine, verified with SHA-256 |
| `create_job` | | | Define a durable, resumable multi-stage job |
| `run_job` | Yes | | Run a job forward, skipping stages that already completed |
| `get_job_status` | Yes | | Per-stage state, log tails, and recent events |
| `list_jobs` | | | List jobs defined on this machine |
| `reset_job_stage` | Yes | | Clear a stage's marker so it runs again |

> **Note:** `execute_cell` was renamed to `run_code_cell` in 2026-06-16 to match the browser-side handler name. Pass a `cellId` (from `add_code_cell` or `get_cells`) — the old `cellIndex` fallback was removed.

## Long-Running Jobs, Runtime Control, and File Transfer

`run_code_cell` returns only when Colab has finished executing, which makes it
unusable for anything long: the MCP call blocks with no progress and clients
eventually give up. The tools below cover that gap.

### Waiting on a long cell

```
add_code_cell(code="train(epochs=50)")        -> {"newCellId": "abc123"}
run_code_cell(cellId="abc123")                 # may exceed the client timeout
wait_for_cell(cellId="abc123", timeout_sec=900)
> {"status": "running" | "completed" | "timeout" | "missing" | "unknown", ...}
```

`wait_for_cell` polls `get_cells` instead of holding the call open. A
`timeout` result means *the wait* expired - the cell keeps running in Colab, so
poll again or call `interrupt_runtime`.

> **Known limitation, confirmed by live testing (2026-08-25).** Colab's
> `get_cells` returns only `{cell_type, id, source}` - no execution state, no
> execution count, no outputs. `wait_for_cell` therefore cannot observe a cell
> that was started **in the browser**, and honestly reports `status: "unknown"`
> with the raw cell attached instead of guessing. It remains useful for cells
> this server started, where `run_code_cell` returns the outputs directly. For
> work that must outlive the connection, use **Durable Jobs** below - they track
> progress through status files in the runtime and do not depend on notebook
> metadata at all.

### Moving files in and out of the runtime

```
upload_file(local_path="./data.parquet", remote_path="/content/data.parquet")
download_file(remote_path="/content/ckpt.pt", local_path="./ckpt.pt")
```

Bytes travel as base64 through a temporary code cell that is deleted afterwards,
and both directions verify a SHA-256 computed on both sides, so a truncated
chunk fails loudly instead of writing a corrupted file. The default ceiling is
25 MB per transfer (`max_bytes`); stage larger artifacts through Drive.

### Runtime status and control

`get_runtime_status` answers by **running a probe snippet in the runtime**, not
by reading the UI: a runtime that executes code is connected, and the reply
carries the GPU, RAM, and free disk that the connect indicator never exposed
reliably. The UI reading is still returned as `indicator`, for diagnosis only.

```
get_runtime_status()
> {"state": "connected", "elapsed_sec": 1.8,
>  "runtime": {"gpu": "Tesla T4, 15360 MiB", "ram_total_gb": 12.7,
>              "disk_free_gb": 78.2, "python": "3.12.11"}, "indicator": {...}}
```

Live testing showed why the UI cannot be trusted here: a *connected* A100
runtime rendered the label `Connect A100High-RAM arrow_drop_down`, because the
connect button's shadow root also carries hidden nodes. The classifier now
reports `unknown` for such labels rather than the wrong answer.

`interrupt_runtime` and `restart_runtime` still drive Colab's own keyboard
shortcuts, and remain **experimental**:

> Live testing on 2026-08-25 found `interrupt_runtime` reporting `requested`
> while the target cell ran to completion - the shortcut did not take effect.
> Do not rely on it yet. `restart_runtime` discards every bit of runtime state:
> variables, pip installs, and files outside Drive.

---

## Durable Jobs

A Colab session is not a reliable host: the tab closes, the laptop sleeps, the
runtime gets recycled. Work started with `run_code_cell` inherits every one of
those failure modes, because it only lives as long as the cell call.

Jobs decouple the work from the connection. Each stage is written to a file in
the runtime and started as a **detached process**, so it keeps running when the
browser goes away, and every finished stage leaves a marker containing the
SHA-256 of its code and of the artifacts it declared.

```
create_job(
  job_id="qtip-run",
  stages_json='[
    {"id": "setup",  "code": "!pip install -q ...",              "artifacts": []},
    {"id": "quant",  "code": "run_quantization()",               "artifacts": ["/content/out/model.pt"]},
    {"id": "eval",   "code": "evaluate()",                       "artifacts": ["/content/out/scores.json"]}
  ]',
  persist_dir="/content/drive/MyDrive/qtip-run"
)

run_job(job_id="qtip-run", wait_sec=120)
> {"status": "running", "stage": "quant", ...}   # returns; the stage keeps going
```

What this buys you:

- **Resumable.** `run_job` skips every stage whose marker matches its current
  code, so re-running after a disconnect continues instead of restarting. Change
  a stage's code and only that stage (and what follows) runs again.
- **Verified checkpoints.** Declared artifacts are checksummed by the runtime.
  A stage that exits 0 without producing them counts as failed rather than
  silently succeeding, and `persist_dir` receives a copy whose checksum is
  re-verified after the copy.
- **Survives the browser.** The stage process is detached with
  `start_new_session=True`. Closing the tab, reconnecting with
  `reconnect_notebook`, or restarting this server does not stop it - call
  `get_job_status` afterwards to see what happened while you were gone.
- **Heartbeat.** The remote runner refreshes a status file every 10 seconds. A
  stage whose heartbeat went quiet is reported as `stalled`, which is what a
  recycled runtime looks like from outside.
- **Local audit trail.** Manifests and an append-only event log live in
  `~/.colab-mcp/jobs/<job_id>/` (`%LOCALAPPDATA%\colab-mcp\jobs\` on Windows).

A runtime restart still loses everything not written to `persist_dir`; that is
what makes declaring artifacts and pointing `persist_dir` at mounted Drive worth
the trouble.

> Like the runtime-control tools, the job runner has unit and integration
> coverage locally - the tests run the real runner as a real detached process -
> but has **not** yet been exercised against a live Colab runtime.

---

## Quick Start (Without OAuth)

If you just want the notebook tools (no `change_runtime`):

### 1. Install uv

```bash
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Mac/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Important:** Do NOT use `pip install uv` — that version lacks required features.

### 2. Clone this repo

```bash
git clone https://github.com/aliieroglu/hermes-colab-agent.git
cd hermes-colab-agent
uv sync
```

Playwright uses your installed stable Chrome by default. If Chrome is unavailable, install Playwright's Chromium without sudo and select its channel:

```bash
uv run playwright install chromium
# Add "--browser-channel", "chromium" to the MCP args below.
```

### 3. Configure your MCP client

Add to your `.mcp.json` (Claude Code, Cursor, etc.):

```json
{
  "mcpServers": {
    "colab-proxy-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/hermes-colab-agent", "colab-mcp"],
      "timeout": 30000
    }
  }
}
```

### 4. Use it

1. Restart your editor / reload the MCP client.
2. All 23 non-OAuth tools should appear immediately.
3. **First use only:** call `open_colab_notebook` with the target Drive ID or trusted Colab URL. This opens the dedicated profile **without an MCP token**.
4. In that visible Chrome window, sign in to Google and complete 2FA yourself. Never paste credentials, cookies, or 2FA codes into the agent chat.
5. After the notebook loads while signed in, call `open_colab_browser_connection` with the same Drive ID/URL. With no argument it targets the scratchpad.
6. Accept Chrome's Local Network Access permission for Colab when prompted.
7. After Colab consumes the ephemeral connection fragment, the agent removes it from the current browser-history entry. If secure removal cannot be verified, the connection fails closed.
8. Connection success is reported only after browser-side notebook tools appear **and a real `get_cells` semantic probe succeeds**; then use `add_code_cell`, `run_code_cell`, and the other semantic tools.

The profile persists across server restarts. Do not point `--browser-profile-dir` at your everyday Chrome profile and do not copy cookies into it.

---

## Full Setup (With OAuth + GPU Control)

This enables the `change_runtime` tool, which lets your agent assign GPUs without you touching the browser.

### 1. Create OAuth Credentials

You need a Google Cloud project with OAuth configured. This is a one-time setup (~5 minutes):

1. **Create a GCP project** (or use an existing one):
   ```bash
   gcloud projects create colab-mcp-oauth --name="Colab MCP OAuth"
   ```

2. **Configure OAuth consent screen:**
   - Go to [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)
   - Select "External" > Create
   - App name: `Colab MCP`, add your email as support + developer contact
   - Save through all steps

3. **Add yourself as test user:**
   - On the consent screen page > "Test users" > Add your Google email

4. **Create OAuth client ID:**
   - Go to [Credentials](https://console.cloud.google.com/apis/credentials)
   - Create Credentials > OAuth client ID > Desktop app
   - Download the JSON file
   - Save it somewhere safe (e.g., `~/.config/colab-oauth.json`)

> **Note:** OAuth Client IDs can only be created via the Cloud Console web UI. There is no CLI or API for this.

### 2. Configure MCP with OAuth

```json
{
  "mcpServers": {
    "colab-proxy-mcp": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/hermes-colab-agent",
        "colab-mcp",
        "--client-oauth-config", "/path/to/colab-oauth.json"
      ],
      "timeout": 30000
    }
  }
}
```

### 3. Authorize (first time only)

The first time the server starts, it opens your browser for Google OAuth consent. Sign in, click Allow, done. The token is cached at `~/.colab-mcp-auth-token.json` and auto-refreshes — you won't be asked again.

### 4. Use it

```
Agent: change_runtime(accelerator="T4")
> Runtime changed to T4. Endpoint: gpu-t4-s-xxx

Agent: open_colab_browser_connection()
> Connected. Available notebook tools: add_code_cell, add_text_cell, get_cells, run_code_cell, update_cell, delete_cell, move_cell

Agent: add_code_cell(code="!nvidia-smi")
> {"cellId": "abc123", ...}

Agent: run_code_cell(cellId="abc123")
> Tesla T4, 15GB memory...

Agent: get_cells()
> [{"cellId": "abc123", "code": "!nvidia-smi", "outputs": [...]}]
```

---

## CLI Reference

Once installed (via `uv run` or `uvx git+https://github.com/aliieroglu/hermes-colab-agent`), the `colab-mcp` command supports these flags:

| Flag | Description |
|------|-------------|
| _(none)_ | Start the MCP server (default — reads/writes JSON-RPC on stdin/stdout) |
| `-l DIR`, `--log DIR` | Write logs to `DIR`. Defaults to a temp dir under `%TEMP%` / `$TMPDIR` |
| `-p`, `--enable-proxy` | Enable the runtime proxy that exposes browser-based notebook tools. On by default |
| `--client-oauth-config PATH` | Path to OAuth client-secrets JSON. Enables the experimental `change_runtime` tool |
| `--browser-profile-dir PATH` | Dedicated persistent Playwright profile. Default: `~/.local/share/hermes-colab-agent/chrome-profile` |
| `--browser-channel NAME` | Playwright browser channel. Default: `chrome`; use `chromium` with an installed Playwright Chromium |
| `--browser-executable PATH` | Explicit Chrome/Chromium binary; overrides the configured channel |
| `--browser-headless` | Run headlessly. Complete first login and permissions in visible mode before using this |
| `--browser-cdp-endpoint URL` | Attach to an existing loopback-only Chrome DevTools endpoint (for example `http://127.0.0.1:9222`) instead of launching another browser |
| `--no-playwright-browser` | Disable Playwright browser management. Secure MCP connection opening is unavailable in this mode because the token is never placed in a system-browser command-line URL |
| `--list-running` | Print every currently-running `colab-mcp` server (pid, port, host, start time) and exit. Useful when "Disconnected from the local Colab MCP server" appears |
| `--kill-stale` | Terminate every running `colab-mcp` server, clear its registry entry, and exit. Use this from a regular shell (NOT from inside Claude Code) before starting a fresh session |

The server maintains a tiny registry at `%LOCALAPPDATA%\colab-mcp\registry.json` (Windows) or `~/.colab-mcp/registry.json` (macOS/Linux). Each running instance writes a `{pid, port, host, started_at}` entry on startup and removes it on clean shutdown. Stale entries from crashed processes are pruned automatically the next time `colab-mcp` starts.

## Troubleshooting

### Tools don't appear after setup
- Make sure you're using this fork, not the official repo
- Only define `colab-proxy-mcp` in ONE `.mcp.json` file (not both global and project — dual definitions spawn two server instances and one dies silently)
- Restart your editor after changing `.mcp.json`

### `change_runtime` returns "Runtime API not initialized"
- Check that `--client-oauth-config` is in your `.mcp.json` args
- Check that the OAuth JSON file exists at the specified path
- Look at the server logs for the specific error:
  ```bash
  # Find the latest log
  ls -t $TMPDIR/colab-mcp-logs-*/colab-mcp.*.log | head -1 | xargs cat
  ```
- A healthy log shows: `INFO:Colab API client ready`
- If you see `WARNING:Failed to initialize Colab API client`, check the error message

### Windows: Port blocked error (WinError 10013)
Already fixed in this fork (changed to port 8085). If you still hit it, edit `src/colab_mcp/auth.py` and change `OAUTH_SERVER_PORT` to any open port.

### OAuth says "Access denied"
Add your Google email as a test user in Cloud Console > OAuth consent screen > Test users.

### Browser opens but connection times out
Make sure you have a Colab notebook open in the browser tab that opened. Click "Connect" if prompted.

### Chrome reused an old Colab tab pointing at a dead port

Older releases could leave a Colab tab open with a fragment pointing at a previous server's port. Current releases reuse the managed tab, use `?p=<port>` to distinguish the attempt, and remove the fragment from the current browser-history entry after the proxy attempt consumes it.

This fork mitigates that by appending the current port as a query param (`?p=<port>`) to the Colab URL, so each server instance produces a unique URL that Chrome can't dedupe. If you still hit it after upgrading:

1. Close every `colab.research.google.com` tab in your browser.
2. Retry `open_colab_browser_connection` — it will open a fresh tab pointed at the live server.

### Chrome silently blocks every connection attempt after one previous "Block"

If Chrome shows "Disconnected from the local Colab MCP server" on *every* attempt — including immediately after the page loads, with no permission prompt — and the server logs only `stream ends after 0 bytes` (TCP opens then closes without any HTTP request), the most likely cause is that you previously clicked **"Block"** on the Local Network Access prompt for `colab.research.google.com`. Chrome remembers that choice **per site** and never asks again — every WebSocket attempt is silently cancelled before the handshake. Edge / Firefox / other Chromium profiles are unaffected.

**Fix (Chrome):**
1. Open `chrome://settings/content/siteDetails?site=https%3A%2F%2Fcolab.research.google.com`
2. Find **"Access other devices on the network"** / **"Acceder a otros dispositivos en la red"** / Insecure content
3. Change from **Block** to **Ask**
4. Reload the Colab tab and accept the prompt when it appears.

Quickest reset (clears all Colab site permissions):
1. Open `https://colab.research.google.com`
2. Click the **lock icon** next to the URL
3. Click **"Reset permissions"** / **"Restablecer permisos"**
4. Reload and try again.

This was reproduced and root-caused with a manual E2E test (`scripts/manual_browser_test.py`): Edge connected on first attempt, Chrome timed out indefinitely until the per-site permission was reset.

### Chrome asks for "Permission to access other services and apps on this device" (or Colab says "Disconnected")

When the Colab tab loads, Chrome shows a permission prompt:

> **colab.research.google.com wants — Permission to access other services and apps on this device**

**Click _Allow_.** If you block it, the WebSocket connection from the Colab tab to your local `colab-mcp` server is blocked, the tab shows "Disconnected from the local Colab MCP server", and `open_colab_browser_connection` will time out.

This prompt is Chrome's **Local Network Access** policy: a public site (`https://colab.research.google.com`) is asking to talk to a resource on your local network (`ws://localhost:<port>` where `colab-mcp` is listening). Chrome blocks this by default and asks the user. The "other service" in the prompt is **your own `colab-mcp` server running on your machine** — not external access. The connection is scoped to a one-time token delivered in the URL fragment (`#mcpProxyToken=...`); after the proxy attempt consumes it, Playwright removes that fragment from the current browser-history entry.

Chrome remembers the choice per-site, so you only need to allow it once for `colab.research.google.com`.

### "Disconnected from the local Colab MCP server" — IPv4/IPv6 dual-stack bind (root cause)

If you saw this message on the official `googlecolab/colab-mcp` and assumed it was an orphaned-server issue, the **actual root cause** is different — and is fixed in this fork.

With `host="localhost"` + `port=0`, the `websockets` library binds **two sockets on different ephemeral ports** (one for IPv6 `::1` and one for IPv4 `127.0.0.1`), then reports only one of them as the "server port". The Colab tab opens `ws://localhost:<reported-port>`, Chrome resolves `localhost` to either address family, and connects to a port with **no listener** in 50% of cases. The TCP connection drops with `stream ends after 0 bytes` server-side, the Colab tab shows "Disconnected from the local Colab MCP server" instantly, and the user waits 60s for a generic timeout.

This fork forces IPv4-only (`host="127.0.0.1"`) so there is exactly one socket on exactly one port, and asserts this invariant at startup (raising `RuntimeError` if a future change re-introduces the dual-bind). See [`websocket_server.py`](src/colab_mcp/websocket_server.py) and the tests `test_single_socket_single_port` / `test_default_host_is_ipv4`.

### Orphaned colab-mcp processes (separate issue)

If a Colab tab in your browser shows **"Disconnected from the local Colab MCP server"** and re-clicking *Connect* doesn't help, the cause is almost always one or more **orphaned colab-mcp processes** from previous Claude Code sessions. Each instance picks a random ephemeral port, but your Colab tab only remembers the port from the URL fragment used when it first opened — when that server dies (or you spawn a new Claude Code session with a new server on a different port), the tab keeps trying to reach a dead address.

This fork ships with built-in diagnostics. Run any of these from a **regular shell** (not from inside Claude Code, which is itself running an MCP instance):

```bash
# Show every colab-mcp server currently registered as running
uv run --directory /path/to/hermes-colab-agent colab-mcp --list-running

# Terminate orphaned colab-mcp servers, then exit
uv run --directory /path/to/hermes-colab-agent colab-mcp --kill-stale
```

The server writes a small registry file at `%LOCALAPPDATA%\colab-mcp\registry.json` (Windows) or `~/.colab-mcp/registry.json` (macOS/Linux) listing pid + port for each running instance. On every startup it prunes dead entries automatically, and on clean shutdown it removes its own. If `open_colab_browser_connection` times out from inside Claude Code, the new error message also includes the ports + pids of any peer servers so you can identify which one your browser tab is actually pointed at.

After cleaning up, re-run `open_colab_browser_connection` — it will open a fresh Colab tab pointed at the current (only) server's port + token.

Fixes [upstream issue #84](https://github.com/googlecolab/colab-mcp/discussions/84).

---

## Compatibility

Tested with:
- Claude Code (VS Code extension + CLI)
- Should work with any MCP client that supports the standard tool protocol (Cursor, Windsurf, Codex, etc.)

Supported platforms:
- Windows 10/11
- macOS
- Linux

---

## Changes from Upstream

This fork is based on [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) with these changes:

- **`f70c00d`** Register notebook tools directly on the FastMCP server at startup (fixes invisible tools)
- **`cae498b`** Add `change_runtime` tool with OAuth for programmatic GPU assignment
- **`440e3bc`** Fix `ColabClient` initialization (missing `Prod()` env arg) + change OAuth port to 8085 for Windows
- **`e66ee69`** Match real Colab API signatures (language param, cellId, run_code_cell)
- **Playwright browser bootstrap (Phase 1)** — isolated persistent Chrome profile, URL/Drive-ID notebook selection, matching-tab reuse, token-safe errors/status, concurrent launch protection, and browser lifecycle cleanup.
- **three browser tools** — `get_colab_browser_status`, `list_colab_tabs`, and `open_colab_notebook`; `open_colab_browser_connection` now optionally accepts a notebook URL or Drive ID.
- **stale-server detection** Process registry + `--list-running` / `--kill-stale` flags + clearer timeout diagnostics — fixes [upstream #84](https://github.com/googlecolab/colab-mcp/discussions/84) "Disconnected from the local Colab MCP server"
- **full 7-tool notebook surface** — pre-register `get_cells`, `delete_cell`, `move_cell` (previously missing) and rename `execute_cell` → `run_code_cell` to match the browser-side handler. Closes [upstream #69](https://github.com/googlecolab/colab-mcp/discussions/69).

Google [does not accept external contributions](https://github.com/googlecolab/colab-mcp/blob/main/CONTRIBUTING.md) to the official repo, so these fixes live here.

## Verified fixes (accepted in upstream discussions)

- **[#67 → answered](https://github.com/googlecolab/colab-mcp/discussions/67)** — invisible-tools fix (this fork's pre-registration approach was accepted by the upstream community as the working solution).
- **[#69](https://github.com/googlecolab/colab-mcp/discussions/69)** — follow-up on `get_cells` and the remaining missing stubs — addressed in this fork on 2026-06-16.
- **[#84](https://github.com/googlecolab/colab-mcp/discussions/84)** — "Disconnected from the local Colab MCP server" — addressed via the stale-server registry + `--kill-stale` CLI.

---

## License

Apache 2.0 (same as upstream)

---

⭐ **If this fork saved you time, a star helps others find it.**
