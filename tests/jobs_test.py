"""Tests for durable multi-stage jobs.

The Colab side is emulated by executing the generated helper code locally
against a temp directory, which means these tests exercise the real runner
script as a real detached process: stage files are written, markers and
checksums are produced by the actual code that would run in the runtime, and
resume behaviour is observed rather than mocked.
"""

import asyncio
import json

import pytest

from colab_mcp import jobs
from fake_colab import FakeColab


@pytest.fixture
def local_jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_MCP_JOBS_DIR", str(tmp_path / "jobs"))
    return tmp_path / "jobs"


def make_manifest(tmp_path, stages, *, persist_dir=None):
    return jobs.create_job(
        "demo-job",
        stages,
        persist_dir=persist_dir,
        work_dir=str(tmp_path),
        remote_base=str(tmp_path / "remote"),
    )


async def _fast_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.05))


def test_parse_stages_validates_ids_code_and_artifacts():
    stages = jobs.parse_stages(
        json.dumps([{"id": "prep", "code": "print(1)", "artifacts": ["/content/a"]}])
    )
    assert stages[0].id == "prep"
    assert stages[0].artifacts == ("/content/a",)

    with pytest.raises(jobs.JobError, match="non-empty JSON array"):
        jobs.parse_stages("[]")
    with pytest.raises(jobs.JobError, match="needs an id"):
        jobs.parse_stages('[{"id": "bad id", "code": "x"}]')
    with pytest.raises(jobs.JobError, match="duplicate stage id"):
        jobs.parse_stages('[{"id": "a", "code": "x"}, {"id": "a", "code": "y"}]')
    with pytest.raises(jobs.JobError, match="no code"):
        jobs.parse_stages('[{"id": "a", "code": "   "}]')
    with pytest.raises(jobs.JobError, match="artifacts must be"):
        jobs.parse_stages('[{"id": "a", "code": "x", "artifacts": "one"}]')
    with pytest.raises(jobs.JobError, match="valid JSON"):
        jobs.parse_stages("{not json")


def test_manifest_round_trips_through_disk(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "one", "code": "print(1)"}])

    reloaded = jobs.load_manifest("demo-job")

    assert reloaded == manifest
    assert jobs.list_job_ids() == ["demo-job"]
    assert jobs.describe_job(reloaded)["stages"][0]["code_sha"] == (
        manifest.stages[0].code_sha()
    )
    # The description is for the agent, so it carries hashes rather than code.
    assert "code" not in jobs.describe_job(reloaded)["stages"][0]


def test_load_manifest_reports_unknown_and_corrupt_jobs(local_jobs_dir):
    with pytest.raises(jobs.JobError, match="No job named"):
        jobs.load_manifest("ghost")

    path = jobs.manifest_path("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops")
    with pytest.raises(jobs.JobError, match="unreadable"):
        jobs.load_manifest("broken")


def test_create_job_rejects_an_unusable_job_id(local_jobs_dir):
    with pytest.raises(jobs.JobError, match="job_id"):
        jobs.create_job("../escape", '[{"id": "a", "code": "print(1)"}]')


@pytest.mark.asyncio
async def test_job_runs_stages_in_order_and_records_artifact_checksums(
    tmp_path, local_jobs_dir
):
    out = tmp_path / "artifacts"
    manifest = make_manifest(
        tmp_path,
        [
            {
                "id": "prepare",
                "code": (
                    f"import pathlib\n"
                    f"p = pathlib.Path({str(out)!r})\n"
                    "p.mkdir(parents=True, exist_ok=True)\n"
                    "(p / 'data.txt').write_text('hello')\n"
                    "print('prepared')\n"
                ),
                "artifacts": [str(out / "data.txt")],
            },
            {
                "id": "train",
                "code": (
                    f"import pathlib\n"
                    f"p = pathlib.Path({str(out)!r})\n"
                    "(p / 'model.txt').write_text((p / 'data.txt').read_text().upper())\n"
                    "print('trained')\n"
                ),
                "artifacts": [str(out / "model.txt")],
            },
        ],
    )
    colab = FakeColab()

    result = await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )

    assert result["status"] == "completed", result
    assert result["launched"] == ["prepare", "train"]
    assert (out / "model.txt").read_text() == "HELLO"

    states = {stage["stage"]: stage for stage in result["stages"]}
    marker = states["train"]["marker"]
    assert marker["code_sha"] == manifest.stages[1].code_sha()
    assert marker["artifacts"][str(out / "model.txt")]["bytes"] == 5
    assert "trained" in result["logs"]["train"]


@pytest.mark.asyncio
async def test_completed_stages_are_skipped_when_the_job_is_run_again(
    tmp_path, local_jobs_dir
):
    counter = tmp_path / "runs.txt"
    manifest = make_manifest(
        tmp_path,
        [
            {
                "id": "once",
                "code": (
                    f"import pathlib\n"
                    f"p = pathlib.Path({str(counter)!r})\n"
                    "p.write_text(str(len(p.read_text()) + 1) if p.exists() else '1')\n"
                ),
                "artifacts": [str(counter)],
            }
        ],
    )
    colab = FakeColab()

    first = await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )
    second = await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )

    assert first["launched"] == ["once"]
    assert second["launched"] == []
    assert second["status"] == "completed"
    assert counter.read_text() == "1"


@pytest.mark.asyncio
async def test_changing_stage_code_invalidates_the_marker(tmp_path, local_jobs_dir):
    log = tmp_path / "log.txt"
    code_v1 = f"import pathlib\npathlib.Path({str(log)!r}).write_text('v1')\n"
    manifest = make_manifest(
        tmp_path, [{"id": "step", "code": code_v1, "artifacts": [str(log)]}]
    )
    colab = FakeColab()
    await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )
    assert log.read_text() == "v1"

    updated = make_manifest(
        tmp_path,
        [
            {
                "id": "step",
                "code": f"import pathlib\npathlib.Path({str(log)!r}).write_text('v2')\n",
                "artifacts": [str(log)],
            }
        ],
    )
    result = await jobs.advance_job(
        colab.call_tool, updated, wait_sec=30, poll_interval_sec=0.05, sleep=_fast_sleep
    )

    assert result["launched"] == ["step"]
    assert log.read_text() == "v2"


@pytest.mark.asyncio
async def test_a_failing_stage_stops_the_job_and_keeps_its_log(
    tmp_path, local_jobs_dir
):
    manifest = make_manifest(
        tmp_path,
        [
            {"id": "boom", "code": "raise SystemExit('stage exploded')\n"},
            {"id": "never", "code": "print('unreachable')\n"},
        ],
    )
    colab = FakeColab()

    result = await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "boom"
    assert "stage exploded" in result["logs"]["boom"]
    assert result["launched"] == ["boom"]


@pytest.mark.asyncio
async def test_a_missing_declared_artifact_fails_the_stage(tmp_path, local_jobs_dir):
    manifest = make_manifest(
        tmp_path,
        [
            {
                "id": "forgetful",
                "code": "print('did not write the artifact')\n",
                "artifacts": [str(tmp_path / "expected.bin")],
            }
        ],
    )
    colab = FakeColab()

    result = await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )

    assert result["status"] == "failed"
    status = result["stages"][0]["status"]
    assert status["missing_artifacts"] == [str(tmp_path / "expected.bin")]


@pytest.mark.asyncio
async def test_artifacts_are_copied_to_the_persist_dir_and_verified(
    tmp_path, local_jobs_dir
):
    artifact = tmp_path / "ckpt.bin"
    persist = tmp_path / "drive" / "checkpoints"
    manifest = make_manifest(
        tmp_path,
        [
            {
                "id": "checkpoint",
                "code": (
                    f"import pathlib\n"
                    f"pathlib.Path({str(artifact)!r}).write_bytes(b'weights' * 100)\n"
                ),
                "artifacts": [str(artifact)],
            }
        ],
        persist_dir=str(persist),
    )
    colab = FakeColab()

    result = await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )

    assert result["status"] == "completed"
    assert (persist / "ckpt.bin").read_bytes() == b"weights" * 100
    marker = result["stages"][0]["marker"]
    assert marker["persisted"][str(artifact)] == str(persist / "ckpt.bin")


@pytest.mark.asyncio
async def test_status_and_events_survive_for_a_later_session(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "quick", "code": "print('done')\n"}])
    colab = FakeColab()
    await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )

    # A fresh manifest object, as a later session would load it.
    status = await jobs.job_status(colab.call_tool, jobs.load_manifest("demo-job"))

    assert status["status"] == "completed"
    assert status["stages"][0]["state"] == "completed"
    assert [event["event"] for event in status["events"]] == [
        "created",
        "stage_relaunch_cleanup",
        "stage_launched",
        "job_completed",
    ]


@pytest.mark.asyncio
async def test_reset_stage_forces_a_re_run(tmp_path, local_jobs_dir):
    counter = tmp_path / "count.txt"
    manifest = make_manifest(
        tmp_path,
        [
            {
                "id": "step",
                "code": (
                    f"import pathlib\n"
                    f"p = pathlib.Path({str(counter)!r})\n"
                    "p.write_text('x' * ((len(p.read_text()) + 1) if p.exists() else 1))\n"
                ),
                "artifacts": [str(counter)],
            }
        ],
    )
    colab = FakeColab()
    await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )
    assert counter.read_text() == "x"

    removed = await jobs.reset_stage(colab.call_tool, manifest, "step")
    assert "done/step.json" in removed["removed"]

    await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=30,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )
    assert counter.read_text() == "xx"

    with pytest.raises(jobs.JobError, match="no stage"):
        await jobs.reset_stage(colab.call_tool, manifest, "ghost")


def test_classify_stage_flags_a_dead_heartbeat_as_stalled(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "long", "code": "print(1)\n"}])
    stage = manifest.stages[0]
    state = {
        "now": 10_000.0,
        "status": {"long": {"state": "running", "heartbeat": 10_000.0 - 600}},
        "done": {},
    }

    verdict = jobs.classify_stage(manifest, stage, state)

    assert verdict["state"] == "stalled"
    assert verdict["heartbeat_age_sec"] == 600.0
    assert "runtime was restarted" in verdict["detail"]


def test_classify_stage_keeps_a_fresh_heartbeat_running(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "long", "code": "print(1)\n"}])
    state = {
        "now": 10_000.0,
        "status": {"long": {"state": "running", "heartbeat": 9_995.0}},
        "done": {},
    }

    assert (
        jobs.classify_stage(manifest, manifest.stages[0], state)["state"] == "running"
    )


def test_classify_stage_ignores_a_marker_from_different_code(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "step", "code": "print('new')\n"}])
    state = {"now": 1.0, "status": {}, "done": {"step": {"code_sha": "old-sha"}}}

    verdict = jobs.classify_stage(manifest, manifest.stages[0], state)

    assert verdict["state"] == "stale_marker"


@pytest.mark.asyncio
async def test_advance_job_returns_while_a_stage_keeps_running(
    tmp_path, local_jobs_dir
):
    marker = tmp_path / "started.txt"
    manifest = make_manifest(
        tmp_path,
        [
            {
                "id": "slow",
                "code": (
                    f"import pathlib, time\n"
                    f"pathlib.Path({str(marker)!r}).write_text('go')\n"
                    "time.sleep(30)\n"
                ),
            }
        ],
    )
    colab = FakeColab()

    result = await jobs.advance_job(
        colab.call_tool,
        manifest,
        wait_sec=0.2,
        poll_interval_sec=0.05,
        sleep=_fast_sleep,
    )

    assert result["status"] == "running"
    assert result["stage"] == "slow"
    assert "continues in Colab" in result["detail"]
    assert marker.exists()

    # The detached process outlives the call that started it.
    status = await jobs.job_status(colab.call_tool, manifest)
    assert status["status"] == "running"


@pytest.mark.asyncio
async def test_advance_job_validates_the_poll_interval(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "a", "code": "print(1)\n"}])
    colab = FakeColab()

    with pytest.raises(jobs.JobError, match="poll_interval_sec"):
        await jobs.advance_job(colab.call_tool, manifest, poll_interval_sec=0)


@pytest.mark.asyncio
async def test_runner_is_uploaded_once_and_reused(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "a", "code": "print(1)\n"}])
    colab = FakeColab()

    first = await jobs.ensure_scaffold(colab.call_tool, manifest)
    second = await jobs.ensure_scaffold(colab.call_tool, manifest)

    assert first == {"runner": "uploaded"}
    assert second == {"runner": "present"}


def test_classify_stage_reruns_a_failure_whose_code_changed(tmp_path, local_jobs_dir):
    manifest = make_manifest(tmp_path, [{"id": "step", "code": "print('fixed')\n"}])
    state = {
        "now": 1.0,
        "done": {},
        "status": {"step": {"state": "failed", "code_sha": "sha-of-old-code"}},
    }

    verdict = jobs.classify_stage(manifest, manifest.stages[0], state)

    assert verdict["state"] == "pending"
    assert "no longer applies" in verdict["detail"]


def test_classify_stage_keeps_a_failure_whose_code_is_unchanged(
    tmp_path, local_jobs_dir
):
    manifest = make_manifest(
        tmp_path, [{"id": "step", "code": "raise SystemExit(1)\n"}]
    )
    stage = manifest.stages[0]
    state = {
        "now": 1.0,
        "done": {},
        "status": {"step": {"state": "failed", "code_sha": stage.code_sha()}},
    }

    assert jobs.classify_stage(manifest, stage, state)["state"] == "failed"


def test_classify_stage_treats_success_without_a_marker_as_pending(
    tmp_path, local_jobs_dir
):
    manifest = make_manifest(tmp_path, [{"id": "step", "code": "print(1)\n"}])
    stage = manifest.stages[0]
    state = {
        "now": 1.0,
        "done": {},
        "status": {"step": {"state": "succeeded", "code_sha": stage.code_sha()}},
    }

    verdict = jobs.classify_stage(manifest, stage, state)

    assert verdict["state"] == "pending"
    assert "completion marker" in verdict["detail"]


@pytest.mark.asyncio
async def test_a_fixed_stage_reruns_without_an_explicit_reset(tmp_path, local_jobs_dir):
    out = tmp_path / "out.txt"
    broken = make_manifest(
        tmp_path,
        [{"id": "step", "code": "raise SystemExit('boom')\n", "artifacts": [str(out)]}],
    )
    colab = FakeColab()
    failure = await jobs.advance_job(
        colab.call_tool, broken, wait_sec=30, poll_interval_sec=0.05, sleep=_fast_sleep
    )
    assert failure["status"] == "failed"

    fixed = make_manifest(
        tmp_path,
        [
            {
                "id": "step",
                "code": f"import pathlib\npathlib.Path({str(out)!r}).write_text('ok')\n",
                "artifacts": [str(out)],
            }
        ],
    )
    result = await jobs.advance_job(
        colab.call_tool, fixed, wait_sec=30, poll_interval_sec=0.05, sleep=_fast_sleep
    )

    assert result["status"] == "completed"
    assert out.read_text() == "ok"
