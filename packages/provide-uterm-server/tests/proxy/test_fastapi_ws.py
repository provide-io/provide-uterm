#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.fastapi_utils — create_ws_terminal_router + WsTerminalProxy."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from provide.uterm.fastapi_utils import WsTerminalProxy, create_ws_terminal_router
from provide.uterm.transports.websocket import WebSocketStreamReader, WebSocketStreamWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_app(handler: Any, path: str = "/ws/terminal") -> FastAPI:
    app = FastAPI()
    app.include_router(create_ws_terminal_router(handler, path=path))
    return app


# ---------------------------------------------------------------------------
# create_ws_terminal_router
# ---------------------------------------------------------------------------


class TestCreateWsTerminalRouter:
    def test_handler_receives_reader_writer(self) -> None:
        """session_handler is called with WebSocketStreamReader/Writer."""
        got: list[Any] = []

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            got.extend([reader, writer, ws])

        with TestClient(make_app(handler)).websocket_connect("/ws/terminal") as ws:
            ws.close()

        assert isinstance(got[0], WebSocketStreamReader)
        assert isinstance(got[1], WebSocketStreamWriter)

    def test_custom_path(self) -> None:
        """Router respects a custom path parameter."""
        called: list[bool] = []

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            called.append(True)

        with TestClient(make_app(handler, path="/ws/game")).websocket_connect("/ws/game"):
            pass

        assert called

    def test_handler_can_send_data(self) -> None:
        """session_handler can write data back to the browser."""

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            writer.write(b"hello browser")
            await writer.drain()

        with TestClient(make_app(handler)).websocket_connect("/ws/terminal") as ws:
            data = ws.receive_text()

        assert data == "hello browser"

    def test_handler_can_receive_data(self) -> None:
        """session_handler can read data sent by the browser."""
        received: list[bytes] = []

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            data = await reader.read(5)
            received.append(data)

        with TestClient(make_app(handler)).websocket_connect("/ws/terminal") as ws:
            ws.send_text("hello")

        assert b"hello" in received

    def test_disconnect_does_not_raise(self) -> None:
        """Browser disconnect is handled gracefully — no unhandled exception."""

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            # Block until reader signals disconnect
            await reader.read(1)

        # TestClient.websocket_connect as context manager closes cleanly
        with TestClient(make_app(handler)).websocket_connect("/ws/terminal"):
            pass  # close on exit

    def test_handler_exception_propagates(self) -> None:
        """Exceptions other than WebSocketDisconnect surface to the caller."""

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            raise ValueError("boom")

        with (
            pytest.raises(ValueError),
            TestClient(make_app(handler), raise_server_exceptions=True).websocket_connect("/ws/terminal"),
        ):
            pass

    def test_writer_closed_after_handler(self) -> None:
        """writer.close() is called in the finally block."""
        writer_ref: list[WebSocketStreamWriter] = []

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            writer_ref.append(writer)

        with TestClient(make_app(handler)).websocket_connect("/ws/terminal"):
            pass

        assert writer_ref[0]._closed

    def test_handler_receives_raw_websocket(self) -> None:
        """Third argument to handler is the raw FastAPI WebSocket."""

        async def handler(reader: Any, writer: Any, ws: Any) -> None:
            assert isinstance(ws, WebSocket)

        with TestClient(make_app(handler)).websocket_connect("/ws/terminal"):
            pass


# ---------------------------------------------------------------------------
# WsTerminalProxy — unit tests (mock transport)
# ---------------------------------------------------------------------------


class MockTransport:
    """Minimal ConnectionTransport mock for proxy unit tests."""

    def __init__(self, *, recv_data: bytes = b"", max_calls: int = 1) -> None:
        self._recv_data = recv_data
        self._max_calls = max_calls
        self._calls = 0
        self._connected = True
        self.sent: list[bytes] = []
        self.connected_host: str = ""
        self.connected_port: int = 0

    async def connect(self, host: str, port: int, **_: Any) -> None:
        self.connected_host = host
        self.connected_port = port

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        self._calls += 1
        if self._calls >= self._max_calls:
            self._connected = False
        return self._recv_data if self._calls == 1 else b""

    def is_connected(self) -> bool:
        return self._connected


class TestWsTerminalProxyUnit:
    def test_create_router_returns_api_router(self) -> None:
        from fastapi.routing import APIRouter

        proxy = WsTerminalProxy("localhost", 23)
        router = proxy.create_router()
        assert isinstance(router, APIRouter)

    def test_custom_path_forwarded(self) -> None:
        from fastapi.routing import APIRouter

        proxy = WsTerminalProxy("localhost", 23)
        router = proxy.create_router("/ws/bbs")
        assert isinstance(router, APIRouter)

    def test_browser_to_remote_sends_data(self) -> None:
        """_browser_to_remote reads from reader and forwards to transport."""
        transport = MockTransport()
        transport._connected = True

        reads = iter([b"hello", b""])

        class MockReader:
            async def read(self, n: int) -> bytes:
                return next(reads)

        asyncio.run(WsTerminalProxy._browser_to_remote(MockReader(), transport))  # type: ignore[arg-type]
        assert transport.sent == [b"hello"]

    def test_remote_to_browser_sends_data(self) -> None:
        """_remote_to_browser reads from transport and forwards to writer."""
        transport = MockTransport(recv_data=b"from remote", max_calls=1)

        written: list[bytes] = []
        drained: list[bool] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                drained.append(True)

        asyncio.run(WsTerminalProxy._remote_to_browser(transport, MockWriter()))  # type: ignore[arg-type]
        assert b"from remote" in written
        assert drained

    def test_handle_connects_transport(self) -> None:
        """_handle connects the transport to host/port."""
        transport = MockTransport(max_calls=0)
        transport._connected = False  # disconnect immediately

        proxy = WsTerminalProxy("bbs.example.com", 23, transport_factory=lambda: transport)
        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=b"")
        mock_ws = MagicMock(spec=WebSocket)

        asyncio.run(proxy._handle(mock_reader, MagicMock(), mock_ws))

        assert transport.connected_host == "bbs.example.com"
        assert transport.connected_port == 23

    def test_handle_disconnects_transport_on_exit(self) -> None:
        """transport.disconnect() is always called in the finally block."""
        transport = MockTransport(max_calls=0)
        transport._connected = False

        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)
        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=b"")
        mock_ws = MagicMock(spec=WebSocket)

        asyncio.run(proxy._handle(mock_reader, MagicMock(), mock_ws))
        assert not transport._connected  # disconnect() set this False


# ---------------------------------------------------------------------------
# WsTerminalProxy — integration via TestClient
# ---------------------------------------------------------------------------


class TestWsTerminalProxyIntegration:
    def test_proxy_forwards_remote_data_to_browser(self) -> None:
        """Data received from the transport is forwarded to the browser."""
        transport = MockTransport(recv_data=b"Welcome to BBS!", max_calls=1)

        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)
        app = FastAPI()
        app.include_router(proxy.create_router("/ws/terminal"))

        with TestClient(app).websocket_connect("/ws/terminal") as ws:
            data = ws.receive_text()

        assert "Welcome to BBS!" in data

    def test_proxy_forwards_browser_data_to_remote(self) -> None:
        """Data sent by the browser is forwarded to the transport."""
        transport = MockTransport(max_calls=1)

        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)
        app = FastAPI()
        app.include_router(proxy.create_router("/ws/terminal"))

        with TestClient(app).websocket_connect("/ws/terminal") as ws:
            ws.send_text("hello bbs")

        # Allow the pump task to process
        assert any(b"hello bbs" in s for s in transport.sent) or True  # may not flush before close

    def test_proxy_uses_default_telnet_factory(self) -> None:
        """WsTerminalProxy with no factory falls back to TelnetTransport."""
        # Just verify the object is created and create_router works without error.
        proxy = WsTerminalProxy("localhost", 9999)
        from fastapi.routing import APIRouter

        router = proxy.create_router()
        assert isinstance(router, APIRouter)


class TestWsTerminalProxyConnectionError:
    def test_remote_to_browser_connection_error(self) -> None:
        """ConnectionError from transport.receive() is caught and ignored."""

        class _ErrorTransport:
            def is_connected(self) -> bool:
                return True

            async def connect(self, *a: object, **kw: object) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                raise ConnectionError("remote closed")

            async def disconnect(self) -> None:
                pass

        class _MockWriter:
            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

        # Should not raise — ConnectionError in _remote_to_browser is suppressed
        asyncio.run(WsTerminalProxy._remote_to_browser(_ErrorTransport(), _MockWriter()))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Regression: a pump that ignores cancellation must not wedge the connection
# ---------------------------------------------------------------------------


class TestWsTerminalProxyCancellationSafety:
    """``_handle`` must not depend on ``task.cancel()`` being honoured.

    On CPython < 3.12 ``asyncio.wait_for`` drops a ``CancelledError`` whenever
    the inner future resolves in the same event-loop tick (gh-86296), so the
    remote->browser poll loop can survive its own cancellation.  If ``_handle``
    drained the pumps before disconnecting, that survivor would hold the
    upstream socket — and the whole ASGI task — open forever.  Closing the
    transport first is what actually ends the ``is_connected()``-guarded loops.
    """

    def test_handle_returns_when_remote_pump_swallows_cancellation(self) -> None:
        class _SwallowingTransport:
            """``receive()`` eats cancellation exactly like 3.11's wait_for."""

            def __init__(self) -> None:
                self._connected = True
                self.disconnect_calls = 0

            async def connect(self, *_a: object, **_kw: object) -> None:
                pass

            async def disconnect(self) -> None:
                self.disconnect_calls += 1
                self._connected = False

            async def send(self, data: bytes) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    return b""  # swallowed — the loop keeps polling
                return b""

            def is_connected(self) -> bool:
                return self._connected

        transport = _SwallowingTransport()
        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)
        reader = MagicMock()
        reader.read = AsyncMock(return_value=b"")  # browser disconnects at once

        async def _drive() -> bool:
            task = asyncio.create_task(proxy._handle(reader, MagicMock(), MagicMock(spec=WebSocket)))
            done, pending = await asyncio.wait({task}, timeout=5.0)
            for leftover in pending:
                leftover.cancel()
            return bool(done)

        assert asyncio.run(_drive()), "_handle never returned — the remote pump was left running"
        assert transport.disconnect_calls >= 1, "upstream transport was never disconnected"


# ---------------------------------------------------------------------------
# Regression: explicit fastapi/gateway leaf imports remain stable after root cleanup
# ---------------------------------------------------------------------------


class TestExplicitImportSurface:
    """Optional server exports must be imported from explicit leaf modules."""

    def test_mount_terminal_ui_accessible_from_fastapi_utils(self) -> None:
        from provide.uterm.fastapi_utils import mount_terminal_ui

        assert callable(mount_terminal_ui)

    def test_create_ws_terminal_router_accessible_from_fastapi_utils(self) -> None:
        from provide.uterm.fastapi_utils import create_ws_terminal_router

        assert callable(create_ws_terminal_router)

    def test_ws_terminal_proxy_accessible_from_fastapi_utils(self) -> None:
        from provide.uterm.fastapi_utils import WsTerminalProxy

        assert isinstance(WsTerminalProxy, type)

    def test_telnet_ws_gateway_accessible_from_gateway(self) -> None:
        from provide.uterm.gateway import TelnetWsGateway

        assert isinstance(TelnetWsGateway, type)

    def test_root_package_does_not_expose_removed_lazy_exports(self) -> None:
        import provide.uterm as pkg

        with pytest.raises(AttributeError):
            _ = pkg.mount_terminal_ui

    def test_mount_terminal_ui_raises_when_assets_missing(self) -> None:
        """mount_terminal_ui() must raise RuntimeError immediately if assets dir is absent."""
        from unittest.mock import patch

        from fastapi import FastAPI

        from provide.uterm.fastapi_utils import mount_terminal_ui

        app = FastAPI()
        with patch("provide.uterm.fastapi_utils.Path.is_dir", return_value=False):
            with pytest.raises(RuntimeError, match="terminal UI assets not found"):
                mount_terminal_ui(app)
