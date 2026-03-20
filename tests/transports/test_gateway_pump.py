#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway — pump helpers (tcp↔ws) and TelnetWsGateway."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets
import websockets.server

from provide.terminal.gateway import (
    TelnetWsGateway,
    _normalize_crlf,
    _pipe_ws,
    _tcp_to_ws,
    _write_token,
    _ws_to_tcp,
)

# ---------------------------------------------------------------------------
# Pump helper unit tests
# ---------------------------------------------------------------------------


class TestTcpToWs:
    async def test_forwards_bytes_as_text(self) -> None:
        sent: list[str] = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(b"hello")
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs())
        assert sent == ["hello"]

    async def test_stops_on_eof(self) -> None:
        sent: list[str] = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs())
        assert sent == []


def _async_iter(items):
    """Return an async iterator over *items*."""

    async def _gen():
        for item in items:
            yield item

    return _gen()


class TestWsToTcp:
    async def test_forwards_str_message(self) -> None:
        written: list[bytes] = []
        drained: list[bool] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                drained.append(True)

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["world"]), cast("StreamWriter", MockWriter()))
        assert written == [b"world"]
        assert drained

    async def test_forwards_bytes_message(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter([b"\xff\xfe"]), cast("StreamWriter", MockWriter()))
        assert written == [b"\xff\xfe"]


# ---------------------------------------------------------------------------
# TelnetWsGateway — integration via real local WS server
# ---------------------------------------------------------------------------


async def _start_ws_echo_server(banner: bytes = b"") -> tuple[Any, int]:
    """Start a local WebSocket server that optionally sends a banner then echoes."""

    async def handler(ws: websockets.ServerConnection) -> None:
        if banner:
            await ws.send(banner.decode("latin-1"))
        async for msg in ws:
            await ws.send(msg)

    srv = await websockets.serve(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port


class TestTelnetWsGateway:
    async def test_start_returns_server(self) -> None:
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            assert tcp_srv is not None
            tcp_srv.close()
        finally:
            ws_srv.close()

    async def test_banner_forwarded_to_telnet_client(self) -> None:
        """Data sent by the WS server arrives at the telnet client."""
        banner = b"Welcome to warp!\r\n"
        ws_srv, ws_port = await _start_ws_echo_server(banner=banner)
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server

            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            tcp_srv.close()
        finally:
            ws_srv.close()

        assert banner.decode("utf-8") in data.decode("utf-8")

    async def test_client_data_forwarded_to_ws(self) -> None:
        """Data sent by the telnet client is echoed back via the WS server."""
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server

            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(b"ping")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            tcp_srv.close()
        finally:
            ws_srv.close()

        assert b"ping" in data

    async def test_disconnect_cleans_up(self) -> None:
        """Closing the telnet client closes cleanly without hanging."""
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server

            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.close()
            # Brief pause to let gateway detect disconnect
            await asyncio.sleep(0.1)
            tcp_srv.close()
        finally:
            ws_srv.close()

    async def test_missing_websockets_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        original = sys.modules.get("websockets")
        sys.modules["websockets"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(ImportError, match="websockets"):
                TelnetWsGateway("ws://localhost:9999")
        finally:
            if original is None:
                sys.modules.pop("websockets", None)
            else:
                sys.modules["websockets"] = original


# ---------------------------------------------------------------------------
# _pipe_ws integration
# ---------------------------------------------------------------------------


class TestPipeWs:
    async def test_pipe_ws_connects_and_exits_on_eof(self) -> None:
        """_pipe_ws connects to a WS server and exits cleanly when reader hits EOF."""
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            reader = asyncio.StreamReader()
            reader.feed_eof()  # immediate EOF → _tcp_to_ws exits quickly

            class MockWriter:
                def write(self, data: bytes) -> None:
                    pass

                async def drain(self) -> None:
                    pass

                def close(self) -> None:
                    pass

                async def wait_closed(self) -> None:
                    pass

            # Should complete without hanging
            from asyncio import StreamWriter
            from typing import cast

            await asyncio.wait_for(
                _pipe_ws(reader, cast("StreamWriter", MockWriter()), f"ws://127.0.0.1:{ws_port}"),
                timeout=3.0,
            )
        finally:
            ws_srv.close()


class TestTelnetWsGatewayHandleException:
    async def test_handle_exception_closes_writer(self) -> None:
        """_handle closes writer even when _pipe_ws raises."""
        gw = TelnetWsGateway("ws://localhost")
        reader = MagicMock()
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch("provide.terminal.gateway._pipe_ws", side_effect=RuntimeError("boom")):
            await gw._handle(reader, writer)

        writer.close.assert_called()


# ---------------------------------------------------------------------------
# _ws_to_tcp — resume/control integration
# ---------------------------------------------------------------------------


class TestWsToTcpResume:
    async def test_session_token_intercepted_not_forwarded(self, tmp_path) -> None:
        token_file = tmp_path / "tok"
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        msg = '{"type": "session_token", "token": "tok123"}'
        await _ws_to_tcp(_async_iter([msg]), cast("StreamWriter", MockWriter()), token_file=token_file)
        assert written == []
        assert token_file.read_text() == "tok123"

    async def test_resume_ok_sends_text_to_tcp(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(['{"type": "resume_ok"}']), cast("StreamWriter", MockWriter()))
        assert any(b"Session resumed" in w for w in written)

    async def test_resume_failed_deletes_token(self, tmp_path) -> None:
        token_file = tmp_path / "tok"
        token_file.write_text("old")
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(
            _async_iter(['{"type": "resume_failed"}']),
            cast("StreamWriter", MockWriter()),
            token_file=token_file,
        )
        assert not token_file.exists()

    async def test_plain_text_forwarded(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["hello"]), cast("StreamWriter", MockWriter()))
        assert b"hello" in written[0]


# ---------------------------------------------------------------------------
# _ws_to_tcp — CRLF normalization
# ---------------------------------------------------------------------------


class TestWsToTcpCrlf:
    async def test_bare_lf_becomes_crlf(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["foo\nbar"]), cast("StreamWriter", MockWriter()))
        assert written[0] == b"foo\r\nbar"

    async def test_existing_crlf_not_doubled(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["foo\r\nbar"]), cast("StreamWriter", MockWriter()))
        assert written[0] == b"foo\r\nbar"


# ---------------------------------------------------------------------------
# _normalize_crlf unit tests
# ---------------------------------------------------------------------------


class TestNormalizeCrlf:
    def test_bare_lf_converted(self) -> None:
        assert _normalize_crlf(b"a\nb") == b"a\r\nb"

    def test_crlf_not_doubled(self) -> None:
        assert _normalize_crlf(b"a\r\nb") == b"a\r\nb"

    def test_no_newline_unchanged(self) -> None:
        assert _normalize_crlf(b"hello") == b"hello"


# ---------------------------------------------------------------------------
# _pipe_ws — resume token
# ---------------------------------------------------------------------------


class TestPipeWsResume:
    async def test_token_file_present_sends_resume(self, tmp_path) -> None:
        """When a token file exists, the first WS message should be a resume JSON."""
        received: list[str] = []

        async def handler(ws: websockets.ServerConnection) -> None:
            received.extend([msg if isinstance(msg, str) else msg.decode() async for msg in ws])

        srv = await websockets.serve(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            token_file = tmp_path / "tok"
            _write_token(token_file, "resume_tok_abc")

            reader = asyncio.StreamReader()
            reader.feed_eof()

            class MockWriter:
                def write(self, data: bytes) -> None:
                    pass

                async def drain(self) -> None:
                    pass

                def close(self) -> None:
                    pass

                async def wait_closed(self) -> None:
                    pass

            from asyncio import StreamWriter
            from typing import cast

            await asyncio.wait_for(
                _pipe_ws(reader, cast("StreamWriter", MockWriter()), f"ws://127.0.0.1:{port}", token_file=token_file),
                timeout=3.0,
            )
        finally:
            srv.close()

        assert len(received) >= 1
        first = json.loads(received[0])
        assert first == {"type": "resume", "token": "resume_tok_abc"}

    async def test_no_token_file_sends_no_resume(self) -> None:
        """When no token file, no resume message is sent."""
        received: list[str] = []

        async def handler(ws: websockets.ServerConnection) -> None:
            received.extend([msg if isinstance(msg, str) else msg.decode() async for msg in ws])

        srv = await websockets.serve(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            reader = asyncio.StreamReader()
            reader.feed_eof()

            class MockWriter:
                def write(self, data: bytes) -> None:
                    pass

                async def drain(self) -> None:
                    pass

                def close(self) -> None:
                    pass

                async def wait_closed(self) -> None:
                    pass

            from asyncio import StreamWriter
            from typing import cast

            await asyncio.wait_for(
                _pipe_ws(reader, cast("StreamWriter", MockWriter()), f"ws://127.0.0.1:{port}"),
                timeout=3.0,
            )
        finally:
            srv.close()

        assert received == []
