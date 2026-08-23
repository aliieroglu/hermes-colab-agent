"""Tests for the MCP tool surface added on top of the browser proxy."""

import asyncio
import json

import pytest

import colab_mcp
from colab_mcp import notebook_ops, runtime_ops
from colab_mcp.browser_manager import BrowserNavigationError
from colab_mcp.session import NOT_CONNECTED_MSG
from colab_mcp.websocket_server import COLAB, SCRATCH_PATH

EXPECTED_TOOLS = {
    # browser + connection
    "get_colab_browser_status",
    "list_colab_tabs",
    "open_colab_notebook",
    "open_colab_browser_connection",
    "reconnect_notebook",
    # notebook cells
    "add_code_cell",
    "add_text_cell",
    "get_cells",
    "run_code_cell",
    "update_cell",
    "delete_cell",
    "move_cell",
    "wait_for_cell",
    # runtime
    "get_runtime_status",
    "interrupt_runtime",
    "restart_runtime",
    "change_runtime",
    # files
    "upload_file",
    "download_file",
    # durable jobs
    "create_job",
    "run_job",
    "get_job_status",
    "list_jobs",
    "reset_job_stage",
}


def test_every_tool_is_pre_registered_for_clients_that_ignore_list_changed():
    registered = set(asyncio.run(colab_mcp.mcp.get_tools()).keys())
    assert registered == EXPECTED_TOOLS


def test_resolve_target_prefers_explicit_then_last_opened_then_scratchpad(monkeypatch):
    monkeypatch.setattr(colab_mcp, "_current_target", None)
    assert colab_mcp._resolve_target("drive-id") == "drive-id"
    assert colab_mcp._resolve_target() == f"{COLAB}{SCRATCH_PATH}"

    monkeypatch.setattr(colab_mcp, "_current_target", "remembered-id")
    assert colab_mcp._resolve_target() == "remembered-id"
    assert colab_mcp._resolve_target("explicit-id") == "explicit-id"


@pytest.mark.asyncio
async def test_proxy_call_raises_instead_of_returning_the_stub_message(monkeypatch):
    monkeypatch.setattr(colab_mcp, "_proxy_client", None)

    with pytest.raises(notebook_ops.NotebookOpsError, match=NOT_CONNECTED_MSG[:20]):
        await colab_mcp._proxy_call("get_cells", {})


@pytest.mark.asyncio
async def test_wait_for_cell_tool_reports_the_disconnected_state(monkeypatch):
    monkeypatch.setattr(colab_mcp, "_proxy_client", None)

    result = await colab_mcp.wait_for_cell.fn(cellId="c1")

    assert result == NOT_CONNECTED_MSG


@pytest.mark.asyncio
async def test_wait_for_cell_tool_returns_json_status(monkeypatch):
    async def fake_wait(call_tool, cell_id, **kwargs):
        assert cell_id == "c1"
        assert kwargs["timeout_sec"] == 30
        return {"status": "completed", "cellId": cell_id}

    monkeypatch.setattr(notebook_ops, "wait_for_cell", fake_wait)

    result = await colab_mcp.wait_for_cell.fn(cellId="c1", timeout_sec=30)

    assert json.loads(result) == {"status": "completed", "cellId": "c1"}


@pytest.mark.asyncio
async def test_runtime_tools_explain_that_browser_management_is_disabled(monkeypatch):
    monkeypatch.setattr(colab_mcp, "_browser_manager", None)

    for tool in (
        colab_mcp.get_runtime_status,
        colab_mcp.interrupt_runtime,
        colab_mcp.restart_runtime,
    ):
        assert "disabled" in await tool.fn()


@pytest.mark.asyncio
async def test_runtime_tools_point_at_open_colab_notebook_when_no_tab_matches(
    monkeypatch,
):
    class NoTabManager:
        async def notebook_page(self, target, *, purpose):
            raise BrowserNavigationError(
                f"Could not find the matching Colab tab to {purpose}"
            )

    monkeypatch.setattr(colab_mcp, "_browser_manager", NoTabManager())
    monkeypatch.setattr(colab_mcp, "_current_target", "abcDEF_12345")

    result = await colab_mcp.get_runtime_status.fn()

    assert "open_colab_notebook" in result
    assert "read runtime status" in result


@pytest.mark.asyncio
async def test_runtime_status_tool_returns_the_probe(monkeypatch):
    class Manager:
        def __init__(self):
            self.requested = None

        async def notebook_page(self, target, *, purpose):
            self.requested = target
            return "page-object"

    manager = Manager()
    monkeypatch.setattr(colab_mcp, "_browser_manager", manager)
    monkeypatch.setattr(colab_mcp, "_current_target", "abcDEF_12345")

    async def fake_status(page):
        assert page == "page-object"
        return {"state": "connected", "probe": {"button": True}}

    monkeypatch.setattr(runtime_ops, "read_runtime_status", fake_status)

    result = json.loads(await colab_mcp.get_runtime_status.fn())

    assert result["state"] == "connected"
    assert manager.requested == "abcDEF_12345"


@pytest.mark.asyncio
async def test_control_errors_never_leak_a_connection_url(monkeypatch):
    class LeakyManager:
        async def notebook_page(self, target, *, purpose):
            raise ValueError(
                "https://colab.research.google.com/drive/x#mcpProxyToken=s3cret"
            )

    monkeypatch.setattr(colab_mcp, "_browser_manager", LeakyManager())

    result = await colab_mcp.interrupt_runtime.fn()

    assert "s3cret" not in result
    assert "ValueError" in result


@pytest.mark.asyncio
async def test_file_tools_surface_transfer_errors_as_plain_text(monkeypatch):
    monkeypatch.setattr(colab_mcp, "_proxy_client", None)

    assert NOT_CONNECTED_MSG in await colab_mcp.upload_file.fn(
        local_path=__file__, remote_path="/content/x.py"
    )
    assert "not found" in await colab_mcp.upload_file.fn(
        local_path="/definitely/missing/file.bin", remote_path="/content/x.bin"
    )


@pytest.mark.asyncio
async def test_reconnect_runs_the_full_connect_flow_even_when_marked_connected(
    monkeypatch,
):
    calls = []

    async def fake_connect(target):
        calls.append(target)
        return "Connection successful. Available notebook tools: get_cells."

    class ConnectedProxy:
        def is_connected(self):
            return True

    monkeypatch.setattr(colab_mcp, "_connect_to_notebook", fake_connect)
    monkeypatch.setattr(colab_mcp, "_proxy_client", ConnectedProxy())
    monkeypatch.setattr(colab_mcp, "_current_target", "abcDEF_12345")

    assert "Already connected" in await colab_mcp.open_colab_browser_connection.fn()
    assert calls == []

    result = await colab_mcp.reconnect_notebook.fn()

    assert "Connection successful" in result
    assert calls == ["abcDEF_12345"]


@pytest.mark.asyncio
async def test_job_tools_validate_before_touching_the_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("COLAB_MCP_JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(colab_mcp, "_proxy_client", None)

    assert "non-empty JSON array" in await colab_mcp.create_job.fn(
        job_id="demo", stages_json="[]"
    )
    assert "No job named" in await colab_mcp.run_job.fn(job_id="ghost")
    assert "No job named" in await colab_mcp.get_job_status.fn(job_id="ghost")
    assert json.loads(await colab_mcp.list_jobs.fn()) == []


@pytest.mark.asyncio
async def test_create_job_then_list_reports_stage_checksums(monkeypatch, tmp_path):
    monkeypatch.setenv("COLAB_MCP_JOBS_DIR", str(tmp_path))

    created = json.loads(
        await colab_mcp.create_job.fn(
            job_id="demo",
            stages_json=json.dumps(
                [{"id": "train", "code": "print(1)", "artifacts": ["/content/m.pt"]}]
            ),
            persist_dir="/content/drive/MyDrive/jobs",
        )
    )

    assert created["stages"][0]["id"] == "train"
    assert created["persist_dir"] == "/content/drive/MyDrive/jobs"
    listed = json.loads(await colab_mcp.list_jobs.fn())
    assert listed == [created]


@pytest.mark.asyncio
async def test_run_job_reports_the_disconnected_state(monkeypatch, tmp_path):
    monkeypatch.setenv("COLAB_MCP_JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(colab_mcp, "_proxy_client", None)
    await colab_mcp.create_job.fn(
        job_id="demo", stages_json=json.dumps([{"id": "a", "code": "print(1)"}])
    )

    assert NOT_CONNECTED_MSG in await colab_mcp.run_job.fn(job_id="demo")
