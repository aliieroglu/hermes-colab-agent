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

import asyncio
from types import SimpleNamespace
from colab_mcp import session
from fastmcp import Client
from fastmcp.server.middleware import MiddlewareContext
import pytest
from unittest.mock import patch, AsyncMock, Mock


@pytest.fixture(autouse=True)
def mock_webbrowser(monkeypatch):
    import webbrowser

    mock_open = Mock()
    monkeypatch.setattr(webbrowser, "open_new", mock_open)
    return mock_open


@pytest.fixture
def mock_wss():
    """Provides a mock ColabWebSocketServer instance."""
    return MockColabWebSocketServer()


class MockColabWebSocketServer:
    def __init__(self):
        self.connection_live = asyncio.Event()
        self.read_stream = AsyncMock()
        self.write_stream = AsyncMock()
        self.token = "test-token"
        self.port = 1234

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_proxy_client(mock_wss):
    client = Mock(spec=session.ColabProxyClient)
    client.wss = mock_wss
    client.is_connected.return_value = False
    return client


class TestDirectTools:
    """Tests for the direct tool registration on the mcp server."""

    @pytest.mark.asyncio
    async def test_mcp_has_expected_tools(self):
        from colab_mcp import mcp
        async with Client(mcp) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert tool_names == {
                "open_colab_browser_connection",
                "add_code_cell",
                "add_text_cell",
                "get_cells",
                "run_code_cell",
                "update_cell",
                "delete_cell",
                "move_cell",
                "change_runtime",
                "get_colab_browser_status",
                "list_colab_tabs",
                "open_colab_notebook",
            }

    @pytest.mark.asyncio
    async def test_stub_returns_not_connected_when_no_proxy(self):
        from colab_mcp import mcp
        async with Client(mcp) as client:
            result = await client.call_tool("add_code_cell", {"code": "print('hi')"})
            assert any(
                session.NOT_CONNECTED_MSG in c.text for c in result.content
            )

    @pytest.mark.asyncio
    async def test_connection_tool_opens_requested_notebook_with_playwright(
        self, monkeypatch
    ):
        import colab_mcp
        from colab_mcp.browser_manager import ColabTab

        browser_calls = []
        semantic_probes = []

        class FakeBrowserManager:
            async def open_notebook(self, target, **kwargs):
                browser_calls.append((target, kwargs))
                return ColabTab(
                    title="Notebook",
                    url="https://colab.research.google.com/drive/abcDEF_12345?p=18585",
                    selected=True,
                )

            async def clear_connection_fragment(self, target):
                return None

        class FakeProxyClient:
            def __init__(self):
                self.connected = False
                self.wss = SimpleNamespace(port=18585, token="do-not-log")
                self.proxy_mcp_client = SimpleNamespace(
                    call_tool=self.call_tool
                )

            async def call_tool(self, name, arguments):
                semantic_probes.append((name, arguments))
                return SimpleNamespace(content=[])

            def is_connected(self):
                return self.connected

            async def await_proxy_connection(self):
                self.connected = True

            async def await_tools_ready(self):
                return ["get_cells"]

        monkeypatch.setattr(colab_mcp, "_browser_manager", FakeBrowserManager())
        monkeypatch.setattr(colab_mcp, "_proxy_client", FakeProxyClient())

        async with Client(colab_mcp.mcp) as client:
            result = await client.call_tool(
                "open_colab_browser_connection",
                {"notebook_url_or_drive_id": "abcDEF_12345"},
            )

        assert not result.is_error
        assert browser_calls == [
            (
                "abcDEF_12345",
                {
                    "connection_port": 18585,
                    "connection_token": "do-not-log",
                },
            )
        ]
        assert semantic_probes == [("get_cells", {})]
        assert "Connection successful" in result.content[0].text

    @pytest.mark.asyncio
    async def test_connection_tool_refuses_without_playwright_without_exposing_token(
        self, monkeypatch, mock_webbrowser
    ):
        import colab_mcp

        class FakeProxyClient:
            wss = SimpleNamespace(port=18585, token="do-not-leak")

            def is_connected(self):
                return False

        monkeypatch.setattr(colab_mcp, "_browser_manager", None)
        monkeypatch.setattr(colab_mcp, "_proxy_client", FakeProxyClient())

        async with Client(colab_mcp.mcp) as client:
            result = await client.call_tool("open_colab_browser_connection", {})

        assert not result.is_error
        assert "disabled" in result.content[0].text.lower()
        assert "do-not-leak" not in result.content[0].text
        mock_webbrowser.assert_not_called()

    @pytest.mark.asyncio
    async def test_connection_fragment_is_cleared_after_connection_attempt(self, monkeypatch):
        import colab_mcp

        events = []

        class FakeBrowserManager:
            async def open_notebook(self, target, **kwargs):
                events.append("open")

            async def clear_connection_fragment(self, target):
                events.append(("clear", target))

        class FakeProxyClient:
            connected = False
            wss = SimpleNamespace(port=18585, token="do-not-leak")

            def is_connected(self):
                return self.connected

            async def await_proxy_connection(self):
                events.append("await")

        monkeypatch.setattr(colab_mcp, "_browser_manager", FakeBrowserManager())
        monkeypatch.setattr(colab_mcp, "_proxy_client", FakeProxyClient())
        monkeypatch.setattr(colab_mcp.process_registry, "list_running", lambda: [])

        async with Client(colab_mcp.mcp) as client:
            result = await client.call_tool(
                "open_colab_browser_connection",
                {"notebook_url_or_drive_id": "abcDEF_12345"},
            )

        assert not result.is_error
        assert events == ["open", "await", ("clear", "abcDEF_12345")]

    @pytest.mark.asyncio
    async def test_fragment_cleanup_warning_does_not_log_token(
        self, monkeypatch, caplog
    ):
        import colab_mcp

        class FakeBrowserManager:
            async def open_notebook(self, target, **kwargs):
                return None

            async def clear_connection_fragment(self, target):
                raise RuntimeError(
                    "failed at #mcpProxyToken=do-not-leak&mcpProxyPort=18585"
                )

        class FakeProxyClient:
            connected = False
            wss = SimpleNamespace(port=18585, token="do-not-leak")

            def is_connected(self):
                return self.connected

            async def await_proxy_connection(self):
                return None

        monkeypatch.setattr(colab_mcp, "_browser_manager", FakeBrowserManager())
        monkeypatch.setattr(colab_mcp, "_proxy_client", FakeProxyClient())
        monkeypatch.setattr(colab_mcp.process_registry, "list_running", lambda: [])
        caplog.set_level("WARNING")

        async with Client(colab_mcp.mcp) as client:
            result = await client.call_tool(
                "open_colab_browser_connection",
                {"notebook_url_or_drive_id": "abcDEF_12345"},
            )

        assert not result.is_error
        assert "do-not-leak" not in caplog.text
        assert "mcpProxyToken" not in caplog.text

    @pytest.mark.asyncio
    async def test_fragment_cleanup_failure_prevents_success(self, monkeypatch):
        import colab_mcp

        class FakeBrowserManager:
            async def open_notebook(self, target, **kwargs):
                return None

            async def clear_connection_fragment(self, target):
                raise RuntimeError("cleanup failed")

        class FakeProxyClient:
            connected = False
            wss = SimpleNamespace(port=18585, token="do-not-leak")
            proxy_mcp_client = AsyncMock()

            def is_connected(self):
                return self.connected

            async def await_proxy_connection(self):
                self.connected = True

            async def await_tools_ready(self):
                return ["get_cells"]

        monkeypatch.setattr(colab_mcp, "_browser_manager", FakeBrowserManager())
        monkeypatch.setattr(colab_mcp, "_proxy_client", FakeProxyClient())

        async with Client(colab_mcp.mcp) as client:
            result = await client.call_tool(
                "open_colab_browser_connection",
                {"notebook_url_or_drive_id": "abcDEF_12345"},
            )

        assert not result.is_error
        assert "Connection successful" not in result.content[0].text
        assert "securely clear" in result.content[0].text


class TestAwaitToolsReady:
    """Tests for await_tools_ready polling."""

    @pytest.mark.asyncio
    async def test_returns_tool_names(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        client.proxy_mcp_client = AsyncMock()
        mock_tool = Mock()
        mock_tool.name = "add_code_cell"
        client.proxy_mcp_client.list_tools = AsyncMock(return_value=[mock_tool])

        result = await client.await_tools_ready()
        assert result == ["add_code_cell"]

    @pytest.mark.asyncio
    async def test_polls_until_available(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        client.proxy_mcp_client = AsyncMock()
        mock_tool = Mock()
        mock_tool.name = "run_code_cell"
        client.proxy_mcp_client.list_tools = AsyncMock(
            side_effect=[[], [mock_tool]]
        )

        with patch("colab_mcp.session.TOOLS_READY_POLL_INTERVAL", 0.01):
            result = await client.await_tools_ready()
        assert result == ["run_code_cell"]

    @pytest.mark.asyncio
    async def test_not_connected(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        result = await client.await_tools_ready()
        assert result == []


class TestColabProxyMiddleware:
    @pytest.mark.asyncio
    async def test_connection_live(self, mock_proxy_client):
        """Tests connection state change from disconnected to connected."""
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = True
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_not_live(self, mock_proxy_client):
        """Tests connection state change from connected to disconnected."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = False
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        assert middleware.last_message_connected is False
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_connection_change(self, mock_proxy_client):
        """Tests no connection state change."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_call_tool_await_connection(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.side_effect = [False, True]
        mock_proxy_client.await_proxy_connection = AsyncMock()
        mock_proxy_client.await_tools_ready = AsyncMock(
            return_value=["add_code_cell", "run_code_cell"]
        )
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        mock_proxy_client.await_tools_ready.assert_called_once()
        context.fastmcp_context.report_progress.assert_called()
        context.fastmcp_context.send_tool_list_changed.assert_called_once()
        assert result.structured_content["result"] is True
        assert "add_code_cell" in result.structured_content["available_tools"]

    @pytest.mark.asyncio
    async def test_on_call_tool_connected_but_no_tools(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.side_effect = [False, True]
        mock_proxy_client.await_proxy_connection = AsyncMock()
        mock_proxy_client.await_tools_ready = AsyncMock(return_value=[])
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        assert result.structured_content["result"] is False
        assert result.structured_content["available_tools"] == []
        assert "not ready" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_on_call_tool_timeout(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.return_value = False
        mock_proxy_client.await_proxy_connection = AsyncMock()
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        assert result.structured_content == {"result": False}


class TestInjectedTools:
    @pytest.mark.asyncio
    async def test_connected(self, mock_wss):
        mock_wss.connection_live.set()
        proxy_client = session.ColabProxyClient(mock_wss)
        proxy_client.proxy_mcp_client = Mock()
        tools = session._make_injected_tools(proxy_client)
        connection_tool = [t for t in tools if t.name == session.INJECTED_TOOL_NAME][0]
        result = await connection_tool.fn()
        assert result is True

    @pytest.mark.asyncio
    async def test_disconnected_does_not_expose_token_to_system_browser(
        self, mock_wss, mock_webbrowser
    ):
        proxy_client = session.ColabProxyClient(mock_wss)
        tools = session._make_injected_tools(proxy_client)
        connection_tool = [t for t in tools if t.name == session.INJECTED_TOOL_NAME][0]
        result = await connection_tool.fn()
        assert result is False
        mock_webbrowser.assert_not_called()

    def test_has_all_expected_tools(self, mock_wss):
        proxy_client = session.ColabProxyClient(mock_wss)
        tools = session._make_injected_tools(proxy_client)
        tool_names = {t.name for t in tools}
        assert tool_names == {
            session.INJECTED_TOOL_NAME,
            "add_code_cell",
            "add_text_cell",
            "get_cells",
            "run_code_cell",
            "update_cell",
            "delete_cell",
            "move_cell",
        }


class TestColabProxyClient:
    def test_is_connected(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.is_connected() is False
        mock_wss.connection_live.set()
        assert client.is_connected() is False
        client.proxy_mcp_client = Mock()
        assert client.is_connected() is True

    def test_client_factory_connection_live(self, mock_wss):
        mock_wss.connection_live.set()
        client = session.ColabProxyClient(mock_wss)
        client.proxy_mcp_client = Mock()

        assert client.client_factory() is client.proxy_mcp_client

    def test_client_factory_connection_not_live(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.client_factory() is client.stubbed_mcp_client

    @pytest.mark.asyncio
    async def test_await_proxy_connection(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        client._start_task = asyncio.create_task(asyncio.sleep(0.01))
        mock_wss.connection_live.set()
        with patch("colab_mcp.session.UI_CONNECTION_TIMEOUT", 0.1):
            await client.await_proxy_connection()
        await client._start_task

    @pytest.mark.asyncio
    @patch("colab_mcp.session.Client")
    @patch("colab_mcp.session.ColabTransport", spec=session.ColabTransport)
    async def test_start_proxy_client(
        self, mock_colab_transport, mock_client, mock_wss
    ):
        mock_client.return_value.__aenter__ = AsyncMock()
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        async with client:
            await client._start_task

        mock_colab_transport.assert_called_once_with(mock_wss)
        mock_client.assert_called_with(mock_colab_transport.return_value)


class TestColabTransport:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ClientSession")
    async def test_connect_session(self, mock_client_session, mock_wss):
        transport = session.ColabTransport(mock_wss)
        mock_client_session.return_value.__aenter__ = AsyncMock()
        async with transport.connect_session(foo="bar") as client_session:
            assert (
                client_session
                == mock_client_session.return_value.__aenter__.return_value
            )

        mock_client_session.assert_called_once_with(
            mock_wss.read_stream, mock_wss.write_stream, foo="bar"
        )


class TestColabSessionProxy:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ColabWebSocketServer")
    @patch("colab_mcp.session.ColabProxyClient")
    async def test_start_proxy_server(
        self,
        mock_colab_proxy_client,
        mock_colab_web_socket_server,
    ):
        mock_colab_web_socket_server.return_value.__aenter__ = AsyncMock()
        mock_colab_proxy_client.return_value.__aenter__ = AsyncMock()
        proxy = session.ColabSessionProxy()
        await proxy.start_proxy_server()
        mock_colab_proxy_client.assert_called_once()
        assert proxy.proxy_client is not None

    @pytest.mark.asyncio
    async def test_cleanup(self):
        proxy = session.ColabSessionProxy()
        proxy._exit_stack = AsyncMock()
        await proxy.cleanup()
        proxy._exit_stack.aclose.assert_called_once()
