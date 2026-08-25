# Changelog

All notable changes to this fork of [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) are documented here.

This fork follows the upstream `1.0.x` baseline and tags fork-specific work
with the date of the change. Upstream-merged work keeps its own commit
history.

## 2026-08-25 - First live run against a real notebook

The Phase 2/3 tools were exercised against a real Colab notebook
(`qtip_cosmos8b.ipynb`, A100 runtime) for the first time. Five tools failed on
a single root cause, and two assumptions turned out to be wrong.

### Fixed
- **`cellIndex` is required by the browser, and must be >= 0.** Every scratch
  cell was created with `add_code_cell(code, language)` and no `cellIndex`, so
  Colab rejected it with `cellIndex Required` - taking down `upload_file`,
  `download_file`, `run_job`, `get_job_status`, and `reset_job_stage` at once.
  `ScratchCell` now resolves an append index from the current cell count, and
  the test double enforces the real schema so this cannot regress.
- **`add_code_cell` / `add_text_cell` now accept -1 as "append".** The browser
  rejects negative indices, so `add_text_cell`'s documented -1 default could
  never have worked; both tools now resolve a negative index to the cell count
  before the call. `add_code_cell` defaults to appending rather than inserting
  at the top.
- **`get_runtime_status` reported `disconnected` for a live A100 runtime.** The
  connect button's shadow root concatenates hidden nodes, so its text read
  `Connect A100High-RAM arrow_drop_down` while the runtime was perfectly
  usable. The tool now decides by **executing a probe snippet** - a runtime
  that runs code is connected - and returns the GPU, RAM, free disk and Python
  version with it. The UI reading is kept as `indicator` for diagnosis, and a
  label like the one above now classifies as `unknown` instead of wrong.

### Known limitations, now documented rather than assumed
- **`get_cells` carries no execution metadata.** The live payload is
  `{cell_type, id, source}` - no execution state, count, or outputs. So
  `wait_for_cell` cannot observe a cell started in the browser and correctly
  reports `unknown`. Durable jobs remain the supported path for long work.
- **`interrupt_runtime` did not interrupt.** It reported `requested` while the
  target cell ran to completion; the Ctrl+M I chord did not reach the notebook.
  Marked as unreliable in the README pending a fix.

## 2026-08-23 - Durable multi-stage jobs (Phase 3)

Cell-scoped execution ties the work to the connection: close the tab and a
four-hour job dies with it. This adds a job layer that does not.

### Added
- **`create_job` / `run_job` / `get_job_status` / `list_jobs` /
  `reset_job_stage`** and `src/colab_mcp/jobs.py`.
- **Detached execution.** Each stage is written to `<remote_root>/stages/<id>.py`
  and started through a generic uploaded runner with `start_new_session=True`,
  so it outlives the browser tab, a `reconnect_notebook`, and this server.
- **Idempotent resume.** A finished stage leaves `done/<id>.json` holding the
  SHA-256 of its code plus every artifact digest. `run_job` skips stages whose
  marker matches, and re-runs one whose code changed - so recovering from a
  disconnect continues rather than restarts.
- **Verified checkpoints.** Declared artifacts are checksummed in the runtime; a
  stage that exits 0 without producing them is failed, not silently completed.
  `persist_dir` gets a copy whose checksum is re-verified after copying.
- **Heartbeat and audit trail.** The runner refreshes `status/<id>.json` every
  10s; a stage that goes quiet for 90s is reported as `stalled` (the shape a
  recycled runtime takes from outside). Manifests and an append-only event log
  live under `~/.colab-mcp/jobs/<job_id>/`.
- **Bounded waits.** `run_job(wait_sec=...)` always returns - completed, failed,
  or "still running in Colab" - so an agent never blocks open-ended on a job.

### Changed
- `notebook_ops` grew `write_remote_text`, `run_python_json` (base64-framed, so
  log text and newlines cannot corrupt the payload), and a shared
  `_write_remote_bytes` used by both `upload_file` and the job layer.
  `_ScratchCell` is now the public `ScratchCell`.

### Fixed
- A stage re-running because its code changed could be launched twice: the old
  `done` marker still read as "completed" until the new process overwrote it.
  `launch_stage` now clears the previous marker, status, and log first, and
  `advance_job` refuses to launch the same stage twice in one call.
- A stage that failed, was fixed, and re-run stayed failed forever: the old
  `failed` status was read without checking whose code produced it, so
  `run_job` reported the stale failure instead of running the new code.
  Terminal statuses are now matched against the stage's current code hash, and
  a `succeeded` status with no matching completion marker re-runs, since the
  marker - not the status file - is the authority on completion.

### Not yet verified
The job runner has integration coverage locally - the tests execute the real
runner as a real detached process against a temp directory - but has **not**
been exercised against a live Colab runtime.

## 2026-08-23 - Runtime, wait, and file-transfer tools (Phase 2)

Phase 1 made the connection reliable; the tool surface still stopped at cell
CRUD. An agent could start a 40-minute training cell and then had no way to
follow it, no way to recover a dropped tab, and no way to get a checkpoint
back out. This closes those gaps. Tool count goes from 12 to 19.

### Added
- **`wait_for_cell(cellId, timeout_sec, poll_interval_sec)`** - polls
  `get_cells` until the cell stops executing instead of holding the MCP call
  open. Returns `completed` / `running` / `timeout` / `missing` / `unknown`;
  a timeout is the *wait* expiring, not the cell stopping. Colab documents no
  stable execution-state field, so several spellings are checked and an
  unrecognised payload is reported as `unknown` with the raw cell attached.
- **`reconnect_notebook(notebook_url_or_drive_id)`** - re-runs the full
  connect + `get_cells` verification flow, without
  `open_colab_browser_connection`'s "Already connected" short-circuit, for
  when the tab reloaded or the machine slept.
- **`upload_file` / `download_file`** - move files between this machine and
  the runtime as base64 through a scratch cell that is deleted afterwards.
  Both directions compare a SHA-256 computed on both sides, so a truncated
  chunk fails loudly. 25 MB default ceiling, `overwrite` guard on download.
- **`get_runtime_status` / `interrupt_runtime` / `restart_runtime`
  (experimental)** - read the Colab connect indicator and send Colab's own
  keyboard shortcuts (Ctrl+M I, Ctrl+M .) to the managed tab, since no local
  API exposes them. They report `unknown` with the raw probe rather than
  claiming a state they could not confirm.
- New modules `notebook_ops.py` (transport-free notebook logic) and
  `runtime_ops.py` (UI-level control), plus `ColabBrowserManager.notebook_page`
  for token-safe page lookup. 54 new tests; suite is now 132.

### Not yet verified
The three runtime-control tools and the file transfers have unit coverage but
have **not** been exercised against a live Colab notebook. The DOM probe,
the two keyboard shortcuts, and the restart confirmation dialog are the parts
most likely to need adjustment after a real E2E run.

## 2026-08-23 - CI pipeline (Phase C)

### Added
- **GitHub Actions CI** (`.github/workflows/ci.yml`): `ruff check` +
  `ruff format --check` on Ubuntu, the full pytest suite on Ubuntu / macOS /
  Windows, and a build job that produces the wheel + sdist, installs the
  wheel into a clean virtualenv and asserts the `colab-mcp` entry point
  runs. Until now the 78 tests and the linter only ran in a local
  pre-commit hook, so nothing verified pushed commits.
- **Dependabot** (`.github/dependabot.yml`): monthly GitHub Actions and uv
  dependency updates.

### Fixed
- `tests/cli_test.py` patched `HOME` only, so the default-profile assertion
  could not pass on Windows, where `Path.home()` reads `USERPROFILE`. Both
  are now set, making the suite genuinely cross-platform.

### Changed
- Applied `ruff format` to the whole tree (7 files were unformatted), so the
  formatting gate in CI has a clean baseline. Mechanical change only.

## 2026-06-19 — E2E validated against real Colab + move_cell signature correction

After driving the full smoke E2E against a real Colab notebook (see
`scripts/e2e_smoke.py --connect`), all 7 notebook tools were verified
end-to-end with cellIds returned by the actual browser. One signature
guess turned out to be wrong:

### Fixed
- **`move_cell` parameter renamed `toIndex` → `cellIndex`**. The browser
  rejected `toIndex` with:
  > Invalid arguments for move_cell: cellIndex Required (received undefined)
  Real signature is `move_cell(cellId: str, cellIndex: int)`.

### Verified against real Colab (E2E)
- `add_code_cell` returns `{"newCellId": "<id>"}` in the result body (not
  in structured_content). The E2E script now parses that and threads the
  real cellId through downstream calls.
- `get_cells` returns `{"cells": [{"cell_type": "code", "id": "<id>",
  "source": [...]}]}`.
- `run_code_cell(cellId)` returns `{"outputs": [{"output_type": "stream",
  "name": "stdout", "text": ["..."]}]}` after execution.
- `update_cell(cellId, content)`, `move_cell(cellId, cellIndex)`,
  `delete_cell(cellId)` all return `{}` on success.

### Tools added
- `scripts/manual_browser_test.py` — starts the WebSocket server, prints
  the Colab URL, and waits up to 5 min for any browser to connect.
  Used to confirm that the server itself works (Edge connects on first
  attempt) and isolate browser-specific blocks (Chrome permission cache).

### Docs
- New README troubleshooting entry for the Chrome "previously blocked"
  permission cache — the most common cause of persistent "Disconnected"
  on Chrome that is not solved by the dual-stack or PNA fixes.

## 2026-06-18 — Private Network Access (PNA) handshake — the REAL real fix

After the dual-stack bind fix (earlier today) the Colab tab still showed
"Disconnected from the local Colab MCP server" with a generic 60s timeout.
DevTools Network tab on the failing tab revealed the actual cause:

- The WebSocket request stayed at "Provisional headers are shown" with
  Connection Start → Stalled "2.3 days".
- Server log only saw `stream ends after 0 bytes` — Chrome was opening
  the TCP socket and then closing it before sending any HTTP bytes.

This is Chrome's [Private Network Access](https://developer.chrome.com/blog/private-network-access-preflight)
behavior. A public origin (`https://colab.research.google.com`) talking
to a local server (`ws://localhost`) is classified as a "private network
request" and Chrome blocks it unless the server confirms acceptance via
specific CORS headers — both on the CORS preflight AND on the final
WebSocket upgrade response.

The upstream `googlecolab/colab-mcp` does not send these headers, which
is why "Disconnected" hits every user who runs Chrome with default
security settings. The earlier theories (orphaned servers, stale tabs)
were correlated symptoms but not the cause.

### Fixed
- `_validate_authorization` (now also `process_request`) intercepts any
  non-WebSocket request (no `Upgrade: websocket` header) and responds
  204 No Content with:
  - `Access-Control-Allow-Origin: https://colab.research.google.com`
  - `Access-Control-Allow-Methods: GET, OPTIONS`
  - `Access-Control-Allow-Headers: authorization, content-type, sec-websocket-*`
  - `Access-Control-Allow-Private-Network: true`
  - `Access-Control-Allow-Credentials: true`
  - `Access-Control-Max-Age: 86400`
- New `_augment_handshake_response` (`process_response` callback) adds
  the same headers to the 101 Switching Protocols response. Chrome
  re-checks PNA on the upgrade response itself — preflight alone is not
  sufficient.

### Tests added
- `test_cors_preflight_responds_with_pna_headers` — sends a raw HTTP GET
  (no Upgrade) and asserts the 204 response carries PNA + CORS headers.
- `test_websocket_handshake_response_has_pna_header` — completes a real
  WebSocket handshake and asserts the 101 response carries the PNA
  header.

## 2026-06-18 — Root-cause fix for "Disconnected from the local Colab MCP server"

The real cause of [upstream #84](https://github.com/googlecolab/colab-mcp/discussions/84) was not stale servers or stale tabs — it was an **IPv4/IPv6 dual-stack bind bug** in the WebSocket server. With `host="localhost"` + `port=0`, the `websockets` library binds two sockets on **different ephemeral ports** (one IPv6 `::1:X`, one IPv4 `127.0.0.1:Y`), then we report only one of them. When Chrome resolves `ws://localhost:<reported>` to whichever family lost the lottery, it connects to a port with **no listener**, the TCP connection drops with "stream ends after 0 bytes", and the Colab tab shows "Disconnected from the local Colab MCP server" with no retry. The user then waits 60s for a generic timeout.

This was reproduced and root-caused by reading the server logs during a smoke E2E run: lines `server listening on [::1]:52319` and `server listening on 127.0.0.1:52320` for the SAME server instance.

### Fixed
- `ColabWebSocketServer.__init__` default host changed from `"localhost"` to
  `"127.0.0.1"`, forcing IPv4-only bind. Single socket, single port, no
  ambiguity. Chrome resolves `localhost` to `127.0.0.1` on Windows and most
  desktop OSes, so this is transparent for the Colab tab.
- `__aenter__` now validates that every bound socket shares the same port,
  raising `RuntimeError` with diagnostic detail if not. This is a hard
  guard against future regressions (e.g., someone changing `host` back to
  `"localhost"` or to a `(v4, v6)` tuple without picking a fixed port).
- Startup logging now lists every bound socket address (not just the first
  one) plus the URL the Colab tab will actually use.

### Tests added
- `test_single_socket_single_port` — fails if the server binds more than one
  port (the dual-stack regression).
- `test_default_host_is_ipv4` — fails if the default host is set to anything
  other than `"127.0.0.1"`.

## 2026-06-16 — Stale-tab dedup fix + better timeout diagnostics

Chrome dedupes browser tabs by URL canonical, ignoring the `#fragment`. That
meant calling `open_colab_browser_connection` a second time (after the first
server died) would silently reuse the old Colab tab whose fragment pointed at
the dead port, instead of opening a fresh tab for the live server. The old
tab showed "Disconnected from the local Colab MCP server" and the new server
timed out 60s later with no actionable feedback.

### Fixed
- `open_colab_browser_connection` now appends `?p=<port>` to the Colab URL as
  a query param. Because the port is ephemeral and changes per server
  instance, the URL is unique → Chrome cannot dedupe → a fresh tab opens
  every time. The `#fragment` remains the source of truth for the Colab
  browser-side code (token + port), so this is purely a Chrome-side fix.

### Changed
- The timeout error returned by `open_colab_browser_connection` now lists the
  three actual causes (stale tabs, Local Network Access denied, browser not
  opening) with specific remediation, instead of a generic "make sure you
  have a Colab notebook open" message.

### Docs
- README troubleshooting: new entries for **Chrome reused an old Colab tab**
  and **Chrome asks for "Permission to access other services"** (Local
  Network Access prompt), with explanations of why each is required.

## 2026-06-16 — Pre-register full 7-tool notebook surface

Closes the gap reported in [discussion #69](https://github.com/googlecolab/colab-mcp/discussions/69): the post-connection success message advertised seven notebook tools, but only four were pre-registered as stubs. `get_cells`, `delete_cell`, and `move_cell` were unreachable on clients that snapshot tools at startup (Claude Code, Codex). Without `get_cells` the bridge was effectively write-only — an agent could create cells but never read state back.

### Added
- Three new pre-registered tools matching the browser-side handler names:
  - `get_cells()` — read current notebook state (cells, IDs, contents, outputs).
  - `delete_cell(cellId)` — delete a cell by ID.
  - `move_cell(cellId, toIndex)` — move a cell to a new position.
- Matching stubs in `_make_injected_tools` (`session.py`) so all seven tools are
  visible at startup with `NOT_CONNECTED_MSG` until the browser connects.

### Changed (breaking)
- `execute_cell` → renamed to `run_code_cell` to match the browser-side tool
  name directly (no more wrapper). The old `execute_cell` no longer exists.
  Callers must migrate to `run_code_cell(cellId)`. The `cellIndex` fallback
  argument was removed — pass a `cellId` from `add_code_cell` or `get_cells`.

### Caveat
- Parameter signatures for `delete_cell` and `move_cell` are best-guess (the
  browser-side JS handlers are not in this repo). Signatures follow the
  established `cellId`-first pattern used by `update_cell` and `run_code_cell`.
  Adjust in review if the real browser handlers differ.

## 2026-05-14 — Stale-server detection and cleanup

Adds a process registry that solves the "Disconnected from the local Colab MCP server" issue ([upstream discussion #84](https://github.com/googlecolab/colab-mcp/discussions/84)) caused by orphaned servers from prior Claude Code sessions.

### Added
- `src/colab_mcp/process_registry.py` — cross-platform process registry (stdlib only).
  Tracks `{pid, port, host, started_at}` for each running server in:
  - `%LOCALAPPDATA%\colab-mcp\registry.json` on Windows
  - `~/.colab-mcp/registry.json` on macOS/Linux
- CLI flag `--list-running` — print every currently-running `colab-mcp` server.
- CLI flag `--kill-stale` — terminate orphaned `colab-mcp` servers and exit.
- Automatic registration on server startup (after the WebSocket port is bound)
  and unregistration on clean shutdown.
- Automatic pruning of dead registry entries on every startup.

### Changed
- `open_colab_browser_connection` timeout error now reports this server's port
  *and* the PIDs/ports of any peer `colab-mcp` processes, so users can
  immediately recognize the "old browser tab pointed at a dead port" case
  instead of staring at a generic "timed out" message.

### Docs
- README: new **CLI Reference** section, new troubleshooting entry, new row in
  "What's Different" table.
- Changes from Upstream: stale-server detection bullet added.

### Verification
Verified end-to-end on Windows 10 with `uv run` and `uvx`:
- Module loads under uv-managed venv (no import errors).
- `--list-running` correctly reports no servers when none registered.
- `--kill-stale` removes a synthetic registry entry whose PID is dead
  (`Terminated 1 stale colab-mcp server(s): pid=888888 port=55555`) and
  empties the registry file.
- Real `colab-mcp` server startup logs `Registered colab-mcp pid=X port=Y`.
- Clean shutdown removes the server's own entry (subsequent `--list-running`
  shows none).

## Earlier (upstream + previous fork commits)

See `git log` for the full history. Highlights:

- `f70c00d` — pre-register notebook tools at startup (fixes invisible tools in
  MCP clients that don't support `notifications/tools/list_changed`). Originally
  4 stubs; expanded to 7 in the 2026-06-16 entry above.
- `cae498b` — add `change_runtime` tool with OAuth for programmatic GPU
  assignment.
- `e66ee69` — match real Colab API signatures (language param, cellId,
  run_code_cell).
- `440e3bc` — fix `ColabClient` initialization (missing `Prod()` env arg) +
  change OAuth port to 8085 for Windows.
