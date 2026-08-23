"""Durable, resumable multi-stage jobs on a Colab runtime.

A Colab session is not a reliable host: the tab closes, the laptop sleeps, the
runtime is recycled after a few hours. Running a long job through
``run_code_cell`` inherits every one of those failure modes, because the work
only lives as long as the cell call.

This module decouples the work from the connection. Each stage is written to a
file in the runtime and started as a **detached process** through a small
generic runner, so it keeps going when the browser goes away. Progress is
written to status files that a later session can read, and each finished stage
leaves a ``done`` marker containing the SHA-256 of its code and of every
artifact it declared. Re-running a job therefore skips the stages that already
completed with the same code, which is what makes a resumed job idempotent.

The layout inside the runtime is::

    <remote_root>/runner.py           generic stage driver
    <remote_root>/stages/<id>.py      the stage's code
    <remote_root>/status/<id>.json    live state + heartbeat
    <remote_root>/logs/<id>.log       stdout + stderr
    <remote_root>/done/<id>.json      completion marker with checksums

Nothing here talks to MCP directly: every call goes through the injected
``call_tool`` coroutine, exactly as in :mod:`colab_mcp.notebook_ops`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from colab_mcp.notebook_ops import CallTool, NotebookOpsError, run_python_json
from colab_mcp import notebook_ops

DEFAULT_REMOTE_BASE = "/content/.colab_mcp_jobs"

#: A stage whose status file has not been touched for this long is treated as
#: stalled: the runtime was almost certainly restarted underneath it.
HEARTBEAT_STALE_SEC = 90.0

#: How often the remote runner refreshes its heartbeat.
HEARTBEAT_INTERVAL_SEC = 10

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

TERMINAL_STATES = {"succeeded", "failed"}


class JobError(RuntimeError):
    """A job definition or job control operation failed."""


@dataclass(frozen=True)
class JobStage:
    id: str
    code: str
    artifacts: tuple[str, ...] = ()
    timeout_sec: float = 21600.0

    def code_sha(self) -> str:
        return hashlib.sha256(self.code.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "artifacts": list(self.artifacts),
            "timeout_sec": self.timeout_sec,
        }


@dataclass(frozen=True)
class JobManifest:
    job_id: str
    stages: tuple[JobStage, ...]
    remote_root: str
    persist_dir: str | None = None
    notebook: str | None = None
    work_dir: str = "/content"
    created_at: float = field(default_factory=time.time)

    def stage(self, stage_id: str) -> JobStage:
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise JobError(f"Job {self.job_id} has no stage {stage_id!r}")

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "stages": [stage.to_dict() for stage in self.stages],
            "remote_root": self.remote_root,
            "persist_dir": self.persist_dir,
            "notebook": self.notebook,
            "work_dir": self.work_dir,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> JobManifest:
        return cls(
            job_id=data["job_id"],
            stages=tuple(
                JobStage(
                    id=stage["id"],
                    code=stage["code"],
                    artifacts=tuple(stage.get("artifacts", ())),
                    timeout_sec=float(stage.get("timeout_sec", 21600.0)),
                )
                for stage in data["stages"]
            ),
            remote_root=data["remote_root"],
            persist_dir=data.get("persist_dir"),
            notebook=data.get("notebook"),
            work_dir=data.get("work_dir") or "/content",
            created_at=float(data.get("created_at", time.time())),
        )


# --------------------------------------------------------------------------
# Local manifest + event storage
# --------------------------------------------------------------------------


def jobs_root() -> Path:
    """Local directory holding manifests and event logs."""
    override = os.environ.get("COLAB_MCP_JOBS_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "colab-mcp" / "jobs"
    return Path(os.path.expanduser("~")) / ".colab-mcp" / "jobs"


def manifest_path(job_id: str) -> Path:
    return jobs_root() / job_id / "manifest.json"


def events_path(job_id: str) -> Path:
    return jobs_root() / job_id / "events.jsonl"


def save_manifest(manifest: JobManifest) -> Path:
    path = manifest_path(manifest.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    return path


def load_manifest(job_id: str) -> JobManifest:
    path = manifest_path(job_id)
    if not path.is_file():
        raise JobError(f"No job named {job_id!r}. Create it with create_job first.")
    try:
        return JobManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError) as exc:
        raise JobError(f"Job manifest for {job_id!r} is unreadable: {exc}") from None


def list_job_ids() -> list[str]:
    root = jobs_root()
    if not root.is_dir():
        return []
    return sorted(
        entry.name for entry in root.iterdir() if (entry / "manifest.json").is_file()
    )


def append_event(job_id: str, event: dict) -> None:
    """Append one line to the local event log - the server-side heartbeat.

    The log is what tells you, after a crash or a week later, when each stage
    started, finished, or was skipped. Failing to write it must never take a
    running job down, so errors are swallowed deliberately.
    """
    record = {"at": time.time(), **event}
    try:
        path = events_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def read_events(job_id: str, limit: int = 50) -> list[dict]:
    path = events_path(job_id)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


# --------------------------------------------------------------------------
# Job definition
# --------------------------------------------------------------------------


def parse_stages(stages_json: str | list) -> tuple[JobStage, ...]:
    """Validate a stage list supplied as JSON (or an already-parsed list)."""
    if isinstance(stages_json, str):
        try:
            raw = json.loads(stages_json)
        except json.JSONDecodeError as exc:
            raise JobError(f"stages must be valid JSON: {exc}") from None
    else:
        raw = stages_json
    if not isinstance(raw, list) or not raw:
        raise JobError("stages must be a non-empty JSON array of stage objects")

    stages: list[JobStage] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise JobError(f"stage #{index} must be an object")
        stage_id = str(item.get("id", "")).strip()
        if not _ID_RE.fullmatch(stage_id):
            raise JobError(
                f"stage #{index} needs an id of letters, digits, dot, dash or "
                "underscore (max 64 chars)"
            )
        if stage_id in seen:
            raise JobError(f"duplicate stage id {stage_id!r}")
        seen.add(stage_id)
        code = item.get("code", "")
        if not isinstance(code, str) or not code.strip():
            raise JobError(f"stage {stage_id!r} has no code")
        artifacts = item.get("artifacts", [])
        if not isinstance(artifacts, list) or not all(
            isinstance(a, str) and a.strip() for a in artifacts
        ):
            raise JobError(f"stage {stage_id!r}: artifacts must be a list of paths")
        try:
            timeout_sec = float(item.get("timeout_sec", 21600.0))
        except (TypeError, ValueError):
            raise JobError(
                f"stage {stage_id!r}: timeout_sec must be a number"
            ) from None
        stages.append(
            JobStage(
                id=stage_id,
                code=code,
                artifacts=tuple(artifacts),
                timeout_sec=timeout_sec,
            )
        )
    return tuple(stages)


def create_job(
    job_id: str,
    stages_json: str | list,
    *,
    persist_dir: str | None = None,
    notebook: str | None = None,
    work_dir: str = "/content",
    remote_base: str = DEFAULT_REMOTE_BASE,
) -> JobManifest:
    """Define (or redefine) a job and store its manifest locally."""
    if not _ID_RE.fullmatch(job_id or ""):
        raise JobError(
            "job_id must be letters, digits, dot, dash or underscore (max 64 chars)"
        )
    manifest = JobManifest(
        job_id=job_id,
        stages=parse_stages(stages_json),
        remote_root=f"{remote_base.rstrip('/')}/{job_id}",
        persist_dir=(persist_dir or None),
        notebook=(notebook or None),
        work_dir=work_dir or "/content",
    )
    save_manifest(manifest)
    append_event(
        job_id,
        {
            "event": "created",
            "stages": [stage.id for stage in manifest.stages],
            "persist_dir": manifest.persist_dir,
        },
    )
    return manifest


# --------------------------------------------------------------------------
# Remote scaffolding and state
# --------------------------------------------------------------------------

RUNNER_SOURCE = '''"""Generic stage driver uploaded by colab-mcp. Runs detached from the browser."""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

root = pathlib.Path(sys.argv[1])
stage = sys.argv[2]
artifacts = json.loads(sys.argv[3])
code_sha = sys.argv[4]
persist_dir = sys.argv[5] if len(sys.argv) > 5 else ""
work_dir = sys.argv[6] if len(sys.argv) > 6 else "/content"

status_path = root / "status" / (stage + ".json")
log_path = root / "logs" / (stage + ".log")
done_path = root / "done" / (stage + ".json")
state = {
    "stage": stage,
    "state": "running",
    "pid": os.getpid(),
    "code_sha": code_sha,
    "started_at": time.time(),
}


def write_status(**updates):
    state.update(updates)
    state["heartbeat"] = time.time()
    tmp = status_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(str(tmp), str(status_path))


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


write_status()
stop = threading.Event()


def beat():
    while not stop.wait(%(heartbeat)d):
        write_status()


threading.Thread(target=beat, daemon=True).start()

returncode = 1
try:
    with open(str(log_path), "ab", buffering=0) as log:
        returncode = subprocess.call(
            [sys.executable, "-u", str(root / "stages" / (stage + ".py"))],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
        )
finally:
    stop.set()

produced = {}
missing = []
for artifact in artifacts:
    path = pathlib.Path(artifact)
    if path.is_file():
        produced[artifact] = {"sha256": digest(path), "bytes": path.stat().st_size}
    else:
        missing.append(artifact)

persisted = {}
persist_errors = {}
if returncode == 0 and not missing and persist_dir:
    target_dir = pathlib.Path(persist_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts:
            source = pathlib.Path(artifact)
            target = target_dir / source.name
            tmp = target.with_name(target.name + ".part")
            with open(str(source), "rb") as src, open(str(tmp), "wb") as dst:
                for block in iter(lambda: src.read(1 << 20), b""):
                    dst.write(block)
            os.replace(str(tmp), str(target))
            copied = digest(target)
            if copied == produced[artifact]["sha256"]:
                persisted[artifact] = str(target)
            else:
                persist_errors[artifact] = "checksum mismatch after copy"
    except Exception as exc:
        persist_errors["_"] = "%%s: %%s" %% (type(exc).__name__, exc)

finished = {
    "returncode": returncode,
    "artifacts": produced,
    "missing_artifacts": missing,
    "persisted": persisted,
    "persist_errors": persist_errors,
    "finished_at": time.time(),
}

if returncode == 0 and not missing and not persist_errors:
    done_path.write_text(
        json.dumps(
            {
                "stage": stage,
                "code_sha": code_sha,
                "artifacts": produced,
                "persisted": persisted,
                "finished_at": finished["finished_at"],
            }
        )
    )
    write_status(state="succeeded", **finished)
else:
    write_status(state="failed", **finished)
''' % {"heartbeat": HEARTBEAT_INTERVAL_SEC}


def _literal(value: str) -> str:
    return json.dumps(str(value))


async def ensure_scaffold(call_tool: CallTool, manifest: JobManifest) -> dict:
    """Create the remote job directories and upload the runner if needed."""
    root = _literal(manifest.remote_root)
    expected = hashlib.sha256(RUNNER_SOURCE.encode("utf-8")).hexdigest()
    probe = await run_python_json(
        call_tool,
        "import hashlib, pathlib\n"
        f"_root = pathlib.Path({root})\n"
        "for _sub in ('stages', 'status', 'logs', 'done'):\n"
        "    (_root / _sub).mkdir(parents=True, exist_ok=True)\n"
        "_runner = _root / 'runner.py'\n"
        "_result = {'runner_sha': hashlib.sha256(_runner.read_bytes()).hexdigest()\n"
        "           if _runner.is_file() else None}\n",
    )
    if probe.get("runner_sha") != expected:
        await notebook_ops.write_remote_text(
            call_tool, f"{manifest.remote_root}/runner.py", RUNNER_SOURCE
        )
        return {"runner": "uploaded"}
    return {"runner": "present"}


async def read_job_state(
    call_tool: CallTool, manifest: JobManifest, *, log_tail_chars: int = 1500
) -> dict:
    """Read every status file, done marker, and log tail in one round trip."""
    root = _literal(manifest.remote_root)
    return await run_python_json(
        call_tool,
        "import json, pathlib, time\n"
        f"_root = pathlib.Path({root})\n"
        "_result = {'now': time.time(), 'exists': _root.is_dir(),\n"
        "           'status': {}, 'done': {}, 'logs': {}}\n"
        "def _read(_p):\n"
        "    try:\n"
        "        return json.loads(_p.read_text())\n"
        "    except Exception as _exc:\n"
        "        return {'unreadable': str(_exc)}\n"
        "if _root.is_dir():\n"
        "    for _p in sorted((_root / 'status').glob('*.json')):\n"
        "        _result['status'][_p.stem] = _read(_p)\n"
        "    for _p in sorted((_root / 'done').glob('*.json')):\n"
        "        _result['done'][_p.stem] = _read(_p)\n"
        "    for _p in sorted((_root / 'logs').glob('*.log')):\n"
        f"        _result['logs'][_p.stem] = _p.read_text(errors='replace')[-{log_tail_chars}:]\n",
    )


async def launch_stage(
    call_tool: CallTool, manifest: JobManifest, stage: JobStage
) -> dict:
    """Upload a stage's code and start it as a detached process.

    Any marker, status, or log left by an earlier attempt is removed first.
    Otherwise a stage that is re-running because its code changed would keep
    reading as "already done" from the previous marker until the new process
    happened to overwrite it, and the poll loop would launch it again.
    """
    await reset_stage(call_tool, manifest, stage.id, event="stage_relaunch_cleanup")
    await notebook_ops.write_remote_text(
        call_tool, f"{manifest.remote_root}/stages/{stage.id}.py", stage.code
    )
    root = _literal(manifest.remote_root)
    result = await run_python_json(
        call_tool,
        "import pathlib, subprocess, sys\n"
        f"_root = pathlib.Path({root})\n"
        f"_log = _root / 'logs' / {_literal(stage.id + '.log')}\n"
        "_log.parent.mkdir(parents=True, exist_ok=True)\n"
        "_log.write_bytes(b'')\n"
        "_proc = subprocess.Popen(\n"
        "    [sys.executable, '-u', str(_root / 'runner.py'), str(_root),\n"
        f"     {_literal(stage.id)}, {_literal(json.dumps(list(stage.artifacts)))},\n"
        f"     {_literal(stage.code_sha())}, {_literal(manifest.persist_dir or '')},\n"
        f"     {_literal(manifest.work_dir)}],\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        f"    start_new_session=True, cwd={_literal(manifest.work_dir)})\n"
        "_result = {'pid': _proc.pid}\n",
    )
    append_event(
        manifest.job_id,
        {"event": "stage_launched", "stage": stage.id, "pid": result.get("pid")},
    )
    return result


def classify_stage(
    manifest: JobManifest, stage: JobStage, state: dict, *, now: float | None = None
) -> dict:
    """Decide what a stage's recorded state means for the next action."""
    done = (state.get("done") or {}).get(stage.id)
    status = (state.get("status") or {}).get(stage.id) or {}
    remote_now = now if now is not None else state.get("now") or time.time()

    if isinstance(done, dict) and done.get("code_sha") == stage.code_sha():
        return {"stage": stage.id, "state": "completed", "marker": done}
    if isinstance(done, dict):
        return {
            "stage": stage.id,
            "state": "stale_marker",
            "marker": done,
            "detail": (
                "A previous run completed this stage with different code, so the "
                "marker no longer applies and the stage will run again."
            ),
        }

    recorded = status.get("state")
    if recorded == "running":
        heartbeat = status.get("heartbeat")
        age = (
            remote_now - float(heartbeat)
            if isinstance(heartbeat, (int, float))
            else None
        )
        if age is not None and age > HEARTBEAT_STALE_SEC:
            return {
                "stage": stage.id,
                "state": "stalled",
                "heartbeat_age_sec": round(age, 1),
                "status": status,
                "detail": (
                    "The stage process stopped updating its heartbeat, which "
                    "normally means the Colab runtime was restarted under it. "
                    "Re-run the job to start it again."
                ),
            }
        return {
            "stage": stage.id,
            "state": "running",
            "heartbeat_age_sec": None if age is None else round(age, 1),
            "status": status,
        }
    if recorded in TERMINAL_STATES:
        return {"stage": stage.id, "state": recorded, "status": status}
    return {"stage": stage.id, "state": "pending", "status": status or None}


async def advance_job(
    call_tool: CallTool,
    manifest: JobManifest,
    *,
    wait_sec: float = 120.0,
    poll_interval_sec: float = 10.0,
    log_tail_chars: int = 1500,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Run the job forward: skip finished stages, start and follow the next one.

    Returns as soon as the job finishes, a stage fails, or ``wait_sec`` runs
    out - never open-ended, so an agent stays responsive. Because progress
    lives in the runtime, calling this again simply picks up where it left off,
    including after the browser or this server restarted.
    """
    if poll_interval_sec <= 0:
        raise JobError("poll_interval_sec must be positive")

    await ensure_scaffold(call_tool, manifest)
    deadline = monotonic() + max(wait_sec, 0.0)
    launched: list[str] = []

    while True:
        state = await read_job_state(call_tool, manifest, log_tail_chars=log_tail_chars)
        stages = [classify_stage(manifest, stage, state) for stage in manifest.stages]
        summary = {
            "job_id": manifest.job_id,
            "stages": stages,
            "launched": launched,
            "logs": state.get("logs", {}),
        }

        failed = next((s for s in stages if s["state"] == "failed"), None)
        if failed is not None:
            append_event(
                manifest.job_id, {"event": "stage_failed", "stage": failed["stage"]}
            )
            return {
                **summary,
                "status": "failed",
                "stage": failed["stage"],
                "detail": (
                    f"Stage {failed['stage']} failed. Inspect its log, fix the "
                    "stage code, then call run_job again - completed stages are "
                    "skipped, and a stage whose code changed re-runs."
                ),
            }

        running = next((s for s in stages if s["state"] == "running"), None)
        pending = next(
            (s for s in stages if s["state"] in {"pending", "stalled", "stale_marker"}),
            None,
        )

        if running is None and pending is None:
            append_event(manifest.job_id, {"event": "job_completed"})
            return {**summary, "status": "completed"}

        if running is None:
            stage = manifest.stage(pending["stage"])
            if stage.id in launched:
                # Already started in this call; the runner has not written its
                # first status file yet. Waiting is correct - relaunching would
                # run the stage twice.
                running = {"stage": stage.id, "state": "starting"}
            else:
                await launch_stage(call_tool, manifest, stage)
                launched.append(stage.id)
                summary["launched"] = launched
                running = {"stage": stage.id, "state": "running"}

        remaining = deadline - monotonic()
        if remaining <= 0:
            return {
                **summary,
                "status": "running",
                "stage": running["stage"],
                "detail": (
                    "The wait expired while the stage was still running. The work "
                    "continues in Colab even if this session goes away - call "
                    "run_job or get_job_status again to keep following it."
                ),
            }
        await sleep(min(poll_interval_sec, remaining))


async def job_status(
    call_tool: CallTool, manifest: JobManifest, *, log_tail_chars: int = 1500
) -> dict:
    """Read-only view of a job: per-stage state, logs, and local events."""
    state = await read_job_state(call_tool, manifest, log_tail_chars=log_tail_chars)
    stages = [classify_stage(manifest, stage, state) for stage in manifest.stages]
    if all(s["state"] == "completed" for s in stages):
        status = "completed"
    elif any(s["state"] == "failed" for s in stages):
        status = "failed"
    elif any(s["state"] == "running" for s in stages):
        status = "running"
    elif any(s["state"] == "stalled" for s in stages):
        status = "stalled"
    else:
        status = "pending"
    return {
        "job_id": manifest.job_id,
        "status": status,
        "persist_dir": manifest.persist_dir,
        "remote_root": manifest.remote_root,
        "stages": stages,
        "logs": state.get("logs", {}),
        "events": read_events(manifest.job_id, limit=20),
    }


async def reset_stage(
    call_tool: CallTool,
    manifest: JobManifest,
    stage_id: str,
    *,
    event: str = "stage_reset",
) -> dict:
    """Drop a stage's marker, status, and log so the next run re-executes it."""
    stage = manifest.stage(stage_id)
    root = _literal(manifest.remote_root)
    result = await run_python_json(
        call_tool,
        "import pathlib\n"
        f"_root = pathlib.Path({root})\n"
        f"_stage = {_literal(stage.id)}\n"
        "_removed = []\n"
        "for _rel in ('done/%s.json' % _stage, 'status/%s.json' % _stage,\n"
        "             'logs/%s.log' % _stage):\n"
        "    _p = _root / _rel\n"
        "    if _p.exists():\n"
        "        _p.unlink()\n"
        "        _removed.append(_rel)\n"
        "_result = {'removed': _removed}\n",
    )
    append_event(manifest.job_id, {"event": event, "stage": stage.id})
    return {"job_id": manifest.job_id, "stage": stage.id, **result}


def describe_job(manifest: JobManifest) -> dict:
    """Manifest summary that never echoes stage code back to the agent."""
    return {
        "job_id": manifest.job_id,
        "remote_root": manifest.remote_root,
        "persist_dir": manifest.persist_dir,
        "notebook": manifest.notebook,
        "work_dir": manifest.work_dir,
        "created_at": manifest.created_at,
        "stages": [
            {
                "id": stage.id,
                "artifacts": list(stage.artifacts),
                "timeout_sec": stage.timeout_sec,
                "code_sha": stage.code_sha(),
            }
            for stage in manifest.stages
        ],
    }


__all__ = [
    "JobError",
    "JobManifest",
    "JobStage",
    "NotebookOpsError",
    "advance_job",
    "create_job",
    "describe_job",
    "job_status",
    "list_job_ids",
    "load_manifest",
    "reset_stage",
]
