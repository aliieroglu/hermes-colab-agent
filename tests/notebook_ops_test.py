"""Tests for the notebook operation layer.

The Colab side is emulated by a fake that really executes the generated helper
code against a temp directory, so the upload/download round trips exercise the
actual base64 chunking and SHA-256 verification rather than a mocked stand-in.
"""

import json

import pytest
from fake_colab import FakeColab

from colab_mcp import notebook_ops


def test_parse_tool_payload_handles_json_text_and_empty():
    assert notebook_ops.parse_tool_payload('{"a": 1}') == {"a": 1}
    assert notebook_ops.parse_tool_payload("not json") == "not json"
    assert notebook_ops.parse_tool_payload("   ") is None
    assert notebook_ops.parse_tool_payload(None) is None


def test_extract_cells_accepts_both_documented_shapes():
    assert notebook_ops.extract_cells({"cells": [{"id": "a"}, "junk"]}) == [{"id": "a"}]
    assert notebook_ops.extract_cells([{"id": "b"}]) == [{"id": "b"}]
    assert notebook_ops.extract_cells("nope") == []


def test_find_cell_matches_either_id_spelling():
    cells = [{"id": "a"}, {"cellId": "b"}]
    assert notebook_ops.find_cell(cells, "a") == {"id": "a"}
    assert notebook_ops.find_cell(cells, "b") == {"cellId": "b"}
    assert notebook_ops.find_cell(cells, "c") is None


@pytest.mark.parametrize(
    "cell,expected",
    [
        ({"execution_state": "running"}, "running"),
        ({"executionState": "idle"}, "completed"),
        ({"status": "error"}, "completed"),
        ({"executing": True}, "running"),
        ({"isExecuting": False}, "completed"),
        ({"execution_count": 3}, "completed"),
        ({"outputs": [{"output_type": "stream"}]}, "completed"),
        ({"outputs": []}, "unknown"),
        ({"id": "a", "source": ["x"]}, "unknown"),
    ],
)
def test_cell_execution_state_classification(cell, expected):
    assert notebook_ops.cell_execution_state(cell) == expected


def test_extract_stdout_joins_list_str_and_data_payloads():
    payload = {
        "outputs": [
            {"output_type": "stream", "text": ["a", "b"]},
            {"output_type": "stream", "text": "c"},
            {"output_type": "execute_result", "data": {"text/plain": ["d"]}},
        ]
    }
    assert notebook_ops.extract_stdout(payload) == "abcd"


def test_output_error_reports_traceback():
    payload = {
        "outputs": [
            {"output_type": "error", "ename": "ValueError", "traceback": ["l1", "l2"]}
        ]
    }
    assert notebook_ops.output_error(payload) == "l1\nl2"
    assert notebook_ops.output_error({"outputs": []}) is None


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_wait_for_cell_returns_when_execution_finishes():
    clock = _Clock()
    states = iter(
        [
            {"cells": [{"id": "c1", "executing": True}]},
            {"cells": [{"id": "c1", "executing": True}]},
            {
                "cells": [
                    {"id": "c1", "execution_count": 1, "outputs": [{"text": "ok"}]}
                ]
            },
        ]
    )

    async def call_tool(name, arguments):
        assert name == "get_cells"
        return json.dumps(next(states))

    result = await notebook_ops.wait_for_cell(
        call_tool,
        "c1",
        poll_interval_sec=5,
        sleep=clock.sleep,
        monotonic=clock,
    )

    assert result["status"] == "completed"
    assert result["polls"] == 3
    assert result["waited_sec"] == 10.0


@pytest.mark.asyncio
async def test_wait_for_cell_times_out_without_stopping_the_cell():
    clock = _Clock()

    async def call_tool(name, arguments):
        return json.dumps({"cells": [{"id": "c1", "executing": True}]})

    result = await notebook_ops.wait_for_cell(
        call_tool,
        "c1",
        timeout_sec=10,
        poll_interval_sec=4,
        sleep=clock.sleep,
        monotonic=clock,
    )

    assert result["status"] == "timeout"
    # The last sleep is clamped to the deadline, so the wait does not overshoot.
    assert result["waited_sec"] == 10.0
    assert "keeps" in result["detail"]


@pytest.mark.asyncio
async def test_wait_for_cell_reports_missing_and_unknown_shapes():
    async def missing(name, arguments):
        return json.dumps({"cells": []})

    result = await notebook_ops.wait_for_cell(missing, "c1", poll_interval_sec=1)
    assert result["status"] == "missing"

    async def unknown(name, arguments):
        return json.dumps({"cells": [{"id": "c1", "source": ["print(1)"]}]})

    result = await notebook_ops.wait_for_cell(unknown, "c1", poll_interval_sec=1)
    assert result["status"] == "unknown"
    assert result["cell"] == {"id": "c1", "source": ["print(1)"]}


@pytest.mark.asyncio
async def test_wait_for_cell_validates_arguments():
    async def call_tool(name, arguments):
        raise AssertionError("should not be called")

    with pytest.raises(notebook_ops.NotebookOpsError, match="cellId"):
        await notebook_ops.wait_for_cell(call_tool, "")
    with pytest.raises(notebook_ops.NotebookOpsError, match="poll_interval_sec"):
        await notebook_ops.wait_for_cell(call_tool, "c1", poll_interval_sec=0)


@pytest.mark.asyncio
async def test_upload_then_download_round_trips_binary_content(tmp_path):
    colab = FakeColab()
    payload = bytes(range(256)) * 40
    source = tmp_path / "model.bin"
    source.write_bytes(payload)
    remote = tmp_path / "remote" / "model.bin"

    uploaded = await notebook_ops.upload_file(
        colab.call_tool, source, str(remote), chunk_bytes=100
    )

    assert uploaded["status"] == "uploaded"
    assert uploaded["bytes"] == len(payload)
    assert uploaded["chunks"] > 1
    assert remote.read_bytes() == payload

    destination = tmp_path / "back" / "model.bin"
    downloaded = await notebook_ops.download_file(
        colab.call_tool, str(remote), destination, chunk_bytes=100
    )

    assert downloaded["status"] == "downloaded"
    assert downloaded["sha256"] == uploaded["sha256"]
    assert destination.read_bytes() == payload


@pytest.mark.asyncio
async def test_transfers_remove_their_scratch_cell(tmp_path):
    colab = FakeColab()
    source = tmp_path / "a.txt"
    source.write_text("hello")

    await notebook_ops.upload_file(colab.call_tool, source, str(tmp_path / "r.txt"))

    assert colab.cells == {}
    assert colab.calls[-1][0] == "delete_cell"


@pytest.mark.asyncio
async def test_download_survives_output_split_across_stream_chunks(tmp_path):
    colab = FakeColab(stdout_chunking=7)
    payload = b"chunked output handling" * 20
    remote = tmp_path / "chunky.bin"
    remote.write_bytes(payload)

    result = await notebook_ops.download_file(
        colab.call_tool, str(remote), tmp_path / "out.bin", chunk_bytes=64
    )

    assert (tmp_path / "out.bin").read_bytes() == payload
    assert result["chunks"] > 1


@pytest.mark.asyncio
async def test_upload_rejects_missing_file_and_oversized_file(tmp_path):
    colab = FakeColab()
    with pytest.raises(notebook_ops.NotebookOpsError, match="not found"):
        await notebook_ops.upload_file(
            colab.call_tool, tmp_path / "nope.bin", "/content/nope.bin"
        )

    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * 128)
    with pytest.raises(notebook_ops.NotebookOpsError, match="transfer limit"):
        await notebook_ops.upload_file(
            colab.call_tool, big, "/content/big.bin", max_bytes=64
        )


@pytest.mark.asyncio
async def test_upload_fails_loudly_when_colab_reports_a_different_digest(tmp_path):
    colab = FakeColab()
    source = tmp_path / "a.txt"
    source.write_text("hello")
    original_run = colab.call_tool

    async def corrupt(name, arguments):
        if name == "update_cell" and "write_bytes" in arguments.get("content", ""):
            arguments = dict(arguments)
            arguments["content"] = arguments["content"].replace(
                "_p.write_bytes(base64.b64decode(''.join(_parts)))",
                "_p.write_bytes(b'corrupted')",
            )
        return await original_run(name, arguments)

    with pytest.raises(notebook_ops.NotebookOpsError, match="SHA-256"):
        await notebook_ops.upload_file(corrupt, source, str(tmp_path / "r.txt"))


@pytest.mark.asyncio
async def test_download_refuses_to_clobber_without_overwrite(tmp_path):
    colab = FakeColab()
    remote = tmp_path / "r.txt"
    remote.write_text("remote")
    local = tmp_path / "local.txt"
    local.write_text("precious")

    with pytest.raises(notebook_ops.NotebookOpsError, match="overwrite"):
        await notebook_ops.download_file(colab.call_tool, str(remote), local)
    assert local.read_text() == "precious"

    result = await notebook_ops.download_file(
        colab.call_tool, str(remote), local, overwrite=True
    )
    assert result["status"] == "downloaded"
    assert local.read_text() == "remote"


@pytest.mark.asyncio
async def test_download_reports_missing_remote_file_as_a_colab_error(tmp_path):
    colab = FakeColab()
    with pytest.raises(notebook_ops.NotebookOpsError, match="not a file"):
        await notebook_ops.download_file(
            colab.call_tool, str(tmp_path / "ghost.bin"), tmp_path / "out.bin"
        )


def test_slice_marked_output_requires_both_markers():
    with pytest.raises(notebook_ops.NotebookOpsError, match="truncated"):
        notebook_ops._slice_marked_output("partial output")
