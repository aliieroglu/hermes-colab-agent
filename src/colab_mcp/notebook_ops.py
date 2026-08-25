"""Notebook-level operations layered on the browser-side MCP tool surface.

Everything here works through an injected ``call_tool`` coroutine with the
signature ``call_tool(name: str, arguments: dict) -> str``, which is exactly
what the proxy client exposes. Keeping the logic free of MCP plumbing makes it
directly testable and keeps the transport concerns in ``session.py``.

The browser-side payload shapes are only partially documented by Colab, so the
parsing helpers are deliberately tolerant: when a field this module relies on
is missing, it reports ``unknown`` and hands the raw payload back to the agent
instead of guessing.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
import hashlib
import json
from pathlib import Path
import time
from typing import Any

CallTool = Callable[[str, dict], Awaitable[str]]

#: Default ceiling for a single :func:`upload_file` / :func:`download_file`
#: transfer. Cell payloads travel as base64 inside JSON-RPC messages, so large
#: files are slow and memory-hungry rather than impossible.
MAX_TRANSFER_BYTES = 25 * 1024 * 1024

#: Base64 characters per cell round trip. Colab truncates very large stream
#: outputs, so downloads stay well under the observed limit.
DEFAULT_CHUNK_BYTES = 48_000

_DOWNLOAD_BEGIN = "<<<COLAB_MCP_B64_BEGIN>>>"
_DOWNLOAD_END = "<<<COLAB_MCP_B64_END>>>"


class NotebookOpsError(RuntimeError):
    """A notebook operation failed in a way the agent should surface."""


def parse_tool_payload(text: str) -> Any:
    """Parse a browser tool result, tolerating plain-text responses."""
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def extract_cells(payload: Any) -> list[dict]:
    """Return the cell list from a ``get_cells`` payload of any known shape."""
    if isinstance(payload, dict):
        cells = payload.get("cells")
        if isinstance(cells, list):
            return [cell for cell in cells if isinstance(cell, dict)]
        return []
    if isinstance(payload, list):
        return [cell for cell in payload if isinstance(cell, dict)]
    return []


def find_cell(cells: list[dict], cell_id: str) -> dict | None:
    """Find a cell by id, accepting both ``id`` and ``cellId`` spellings."""
    for cell in cells:
        if cell.get("id") == cell_id or cell.get("cellId") == cell_id:
            return cell
    return None


def cell_execution_state(cell: dict) -> str:
    """Classify a cell as ``completed``, ``running``, or ``unknown``.

    Colab does not document a stable execution-state field, so several
    signals are checked in order of reliability. When none of them is present
    the state is reported as ``unknown`` rather than assumed.
    """
    for key in ("execution_state", "executionState", "status"):
        raw = cell.get(key)
        if isinstance(raw, str):
            value = raw.lower()
            if value in {"running", "busy", "queued", "pending", "executing"}:
                return "running"
            if value in {"idle", "done", "ok", "completed", "finished", "error"}:
                return "completed"

    for key in ("executing", "isExecuting", "running"):
        raw = cell.get(key)
        if isinstance(raw, bool):
            return "running" if raw else "completed"

    for key in ("execution_count", "executionCount"):
        if cell.get(key) is not None:
            return "completed"

    outputs = cell.get("outputs")
    if isinstance(outputs, list) and outputs:
        return "completed"

    return "unknown"


def extract_stdout(payload: Any) -> str:
    """Concatenate stdout-ish text from a ``run_code_cell`` payload."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        outputs = payload.get("outputs")
    elif isinstance(payload, list):
        outputs = payload
    else:
        return ""
    if not isinstance(outputs, list):
        return ""

    chunks: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        text = output.get("text")
        if isinstance(text, list):
            chunks.append("".join(part for part in text if isinstance(part, str)))
        elif isinstance(text, str):
            chunks.append(text)
        data = output.get("data")
        if isinstance(data, dict):
            plain = data.get("text/plain")
            if isinstance(plain, list):
                chunks.append("".join(p for p in plain if isinstance(p, str)))
            elif isinstance(plain, str):
                chunks.append(plain)
    return "".join(chunks)


def output_error(payload: Any) -> str | None:
    """Return the traceback/message of an error output, if the run produced one."""
    outputs = payload.get("outputs") if isinstance(payload, dict) else payload
    if not isinstance(outputs, list):
        return None
    for output in outputs:
        if not isinstance(output, dict):
            continue
        if output.get("output_type") not in {"error", "pyerr"}:
            continue
        traceback = output.get("traceback")
        if isinstance(traceback, list):
            return "\n".join(str(line) for line in traceback)
        name = output.get("ename", "Error")
        return f"{name}: {output.get('evalue', '')}".strip()
    return None


async def wait_for_cell(
    call_tool: CallTool,
    cell_id: str,
    *,
    timeout_sec: float = 600.0,
    poll_interval_sec: float = 3.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll ``get_cells`` until ``cell_id`` stops executing.

    Returns a status dict rather than raising, so an agent can react to a
    timeout without losing the partial outputs collected so far.
    """
    if not cell_id:
        raise NotebookOpsError("wait_for_cell requires a cellId")
    if poll_interval_sec <= 0:
        raise NotebookOpsError("poll_interval_sec must be positive")

    started = monotonic()
    polls = 0
    last_cell: dict | None = None

    while True:
        payload = parse_tool_payload(await call_tool("get_cells", {}))
        polls += 1
        cells = extract_cells(payload)
        cell = find_cell(cells, cell_id)
        elapsed = monotonic() - started

        if cell is None:
            return {
                "status": "missing",
                "cellId": cell_id,
                "waited_sec": round(elapsed, 3),
                "polls": polls,
                "detail": "The cell is no longer present in the notebook.",
            }

        last_cell = cell
        state = cell_execution_state(cell)
        if state in {"completed", "unknown"}:
            return {
                "status": state,
                "cellId": cell_id,
                "waited_sec": round(elapsed, 3),
                "polls": polls,
                "cell": cell,
                "error": output_error(cell),
                **(
                    {
                        "detail": (
                            "The notebook payload carries no execution-state, "
                            "execution-count, or output field for this cell, so "
                            "completion could not be confirmed. The raw cell is "
                            "included above."
                        )
                    }
                    if state == "unknown"
                    else {}
                ),
            }

        if elapsed >= timeout_sec:
            return {
                "status": "timeout",
                "cellId": cell_id,
                "waited_sec": round(elapsed, 3),
                "polls": polls,
                "cell": last_cell,
                "detail": (
                    "The cell was still executing when the wait expired. It keeps "
                    "running in Colab; poll again or call interrupt_runtime."
                ),
            }

        await sleep(min(poll_interval_sec, max(timeout_sec - elapsed, 0.0)))


async def append_index(call_tool: CallTool) -> int:
    """Return an index that appends after the last cell.

    The browser-side ``add_code_cell`` requires ``cellIndex`` and rejects
    negative values, so "append" has to be expressed as the current cell count.
    A notebook we cannot count is better served by 0 than by a failed call.
    """
    try:
        payload = parse_tool_payload(await call_tool("get_cells", {}))
    except Exception:
        return 0
    return len(extract_cells(payload))


class ScratchCell:
    """A code cell used as a scratch buffer and removed afterwards."""

    def __init__(self, call_tool: CallTool):
        self._call_tool = call_tool
        self.cell_id: str | None = None

    async def __aenter__(self) -> ScratchCell:
        index = await append_index(self._call_tool)
        payload = parse_tool_payload(
            await self._call_tool(
                "add_code_cell",
                {"code": "", "cellIndex": index, "language": "python"},
            )
        )
        cell_id = None
        if isinstance(payload, dict):
            cell_id = payload.get("newCellId") or payload.get("cellId")
        if not isinstance(cell_id, str) or not cell_id:
            raise NotebookOpsError(
                f"Could not create a scratch cell; browser returned: {payload!r}"
            )
        self.cell_id = cell_id
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.cell_id is None:
            return
        try:
            await self._call_tool("delete_cell", {"cellId": self.cell_id})
        except Exception:
            # A leftover scratch cell is cosmetic; never mask the real error.
            pass

    async def run(self, code: str) -> Any:
        assert self.cell_id is not None
        await self._call_tool("update_cell", {"cellId": self.cell_id, "content": code})
        payload = parse_tool_payload(
            await self._call_tool("run_code_cell", {"cellId": self.cell_id})
        )
        error = output_error(payload)
        if error:
            raise NotebookOpsError(f"Colab raised while running helper code:\n{error}")
        return payload


def _remote_literal(remote_path: str) -> str:
    return json.dumps(str(remote_path))


async def _write_remote_bytes(
    scratch: ScratchCell,
    data: bytes,
    remote_path: str,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    expected_sha256: str | None = None,
) -> dict:
    """Stream bytes into a runtime file and report the digest Colab computed.

    The digest is always recomputed from the file on disk, so a truncated or
    reordered chunk surfaces here rather than in whatever reads the file later.
    """
    digest = expected_sha256 or hashlib.sha256(data).hexdigest()
    encoded = base64.b64encode(data).decode("ascii")
    chunks = [
        encoded[index : index + chunk_bytes]
        for index in range(0, len(encoded), chunk_bytes)
    ] or [""]

    target = _remote_literal(remote_path)
    await scratch.run(
        "import base64, hashlib, pathlib\n"
        f"_p = pathlib.Path({target}).expanduser()\n"
        "_p.parent.mkdir(parents=True, exist_ok=True)\n"
        "_parts = []\n"
        "print('ready')\n"
    )
    for chunk in chunks:
        await scratch.run(f"_parts.append({json.dumps(chunk)})\nprint('ok')\n")
    payload = await scratch.run(
        "_p.write_bytes(base64.b64decode(''.join(_parts)))\n"
        "del _parts\n"
        "print(hashlib.sha256(_p.read_bytes()).hexdigest())\n"
        "print(_p.stat().st_size)\n"
    )

    lines = [
        line.strip() for line in extract_stdout(payload).splitlines() if line.strip()
    ]
    if len(lines) < 2:
        raise NotebookOpsError(
            f"Write verification produced unexpected output: {lines!r}"
        )
    remote_digest, remote_size = lines[-2], lines[-1]
    if remote_digest != digest:
        raise NotebookOpsError(
            "Write verification failed: the SHA-256 in Colab does not match the "
            f"source bytes ({remote_digest} != {digest})."
        )
    if remote_size.isdigit() and int(remote_size) != len(data):
        raise NotebookOpsError(
            f"Write verification failed: Colab wrote {remote_size} bytes, "
            f"expected {len(data)}."
        )
    return {"sha256": remote_digest, "chunks": len(chunks), "bytes": len(data)}


async def upload_file(
    call_tool: CallTool,
    local_path: str | Path,
    remote_path: str,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    max_bytes: int = MAX_TRANSFER_BYTES,
) -> dict:
    """Copy a local file into the Colab runtime through base64 code cells.

    The transfer is verified with a SHA-256 comparison computed on both sides,
    so a truncated or corrupted chunk fails loudly instead of silently
    producing a broken file.
    """
    source = Path(local_path).expanduser()
    if not source.is_file():
        raise NotebookOpsError(f"Local file not found: {source}")
    size = source.stat().st_size
    if size > max_bytes:
        raise NotebookOpsError(
            f"{source} is {size} bytes, above the {max_bytes}-byte transfer limit. "
            "Raise max_bytes deliberately, or stage the file through Drive."
        )
    if chunk_bytes <= 0:
        raise NotebookOpsError("chunk_bytes must be positive")

    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    async with ScratchCell(call_tool) as scratch:
        written = await _write_remote_bytes(
            scratch, data, remote_path, chunk_bytes=chunk_bytes, expected_sha256=digest
        )
    return {
        "status": "uploaded",
        "local_path": str(source),
        "remote_path": remote_path,
        "bytes": size,
        "sha256": written["sha256"],
        "chunks": written["chunks"],
    }


async def download_file(
    call_tool: CallTool,
    remote_path: str,
    local_path: str | Path,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    max_bytes: int = MAX_TRANSFER_BYTES,
    overwrite: bool = False,
) -> dict:
    """Copy a file out of the Colab runtime through base64 cell output."""
    destination = Path(local_path).expanduser()
    if destination.exists() and not overwrite:
        raise NotebookOpsError(
            f"{destination} already exists. Pass overwrite=True to replace it."
        )
    if chunk_bytes <= 0:
        raise NotebookOpsError("chunk_bytes must be positive")

    target = _remote_literal(remote_path)
    collected: list[str] = []
    async with ScratchCell(call_tool) as scratch:
        payload = await scratch.run(
            "import base64, hashlib, pathlib\n"
            f"_p = pathlib.Path({target}).expanduser()\n"
            "assert _p.is_file(), f'not a file: {_p}'\n"
            "_b = _p.read_bytes()\n"
            "_e = base64.b64encode(_b).decode()\n"
            "print(len(_b))\n"
            "print(len(_e))\n"
            "print(hashlib.sha256(_b).hexdigest())\n"
        )
        header = [
            line.strip()
            for line in extract_stdout(payload).splitlines()
            if line.strip()
        ]
        if len(header) < 3:
            raise NotebookOpsError(
                f"Could not read remote file metadata; Colab returned: {header!r}"
            )
        raw_size, encoded_len, remote_digest = header[-3], header[-2], header[-1]
        if not raw_size.isdigit() or not encoded_len.isdigit():
            raise NotebookOpsError(f"Unexpected remote file metadata: {header[-3:]!r}")
        size = int(raw_size)
        if size > max_bytes:
            raise NotebookOpsError(
                f"{remote_path} is {size} bytes, above the {max_bytes}-byte "
                "transfer limit. Raise max_bytes deliberately, or stage the file "
                "through Drive."
            )

        for offset in range(0, int(encoded_len), chunk_bytes):
            payload = await scratch.run(
                f"print({_DOWNLOAD_BEGIN!r})\n"
                f"print(_e[{offset}:{offset + chunk_bytes}])\n"
                f"print({_DOWNLOAD_END!r})\n"
            )
            collected.append(_slice_marked_output(extract_stdout(payload)))
        await scratch.run("del _b, _e\nprint('done')\n")

    encoded = "".join(collected)
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise NotebookOpsError(f"Downloaded data is not valid base64: {exc}") from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != remote_digest:
        raise NotebookOpsError(
            "Download verification failed: the SHA-256 of the received bytes does "
            f"not match Colab's ({digest} != {remote_digest})."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return {
        "status": "downloaded",
        "remote_path": remote_path,
        "local_path": str(destination),
        "bytes": len(data),
        "sha256": digest,
        "chunks": len(collected),
    }


def _slice_marked_output(stdout: str) -> str:
    """Pull one base64 slice out of stdout, ignoring unrelated notebook noise."""
    if _DOWNLOAD_BEGIN not in stdout or _DOWNLOAD_END not in stdout:
        raise NotebookOpsError(
            "Chunk markers missing from cell output; the download was truncated."
        )
    body = stdout.split(_DOWNLOAD_BEGIN, 1)[1].split(_DOWNLOAD_END, 1)[0]
    return "".join(body.split())


async def write_remote_text(
    call_tool: CallTool,
    remote_path: str,
    text: str,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> str:
    """Write a UTF-8 file into the runtime and return its verified SHA-256."""
    data = text.encode("utf-8")
    async with ScratchCell(call_tool) as scratch:
        written = await _write_remote_bytes(
            scratch, data, remote_path, chunk_bytes=chunk_bytes
        )
    return written["sha256"]


async def run_python_json(
    call_tool: CallTool, code: str, *, result_var: str = "_result"
) -> Any:
    """Run helper code in a scratch cell and read back ``result_var`` as JSON.

    The payload comes back base64-encoded between markers, so notebook noise,
    newlines inside log text, and stream chunking cannot corrupt it.
    """
    wrapped = (
        f"{code}\n"
        "import base64 as _b64, json as _json\n"
        f"print({_DOWNLOAD_BEGIN!r})\n"
        f"print(_b64.b64encode(_json.dumps({result_var}).encode()).decode())\n"
        f"print({_DOWNLOAD_END!r})\n"
    )
    async with ScratchCell(call_tool) as scratch:
        payload = await scratch.run(wrapped)
    encoded = _slice_marked_output(extract_stdout(payload))
    try:
        return json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except Exception as exc:
        raise NotebookOpsError(
            f"Helper code returned a payload this server could not decode: {exc}"
        ) from None
