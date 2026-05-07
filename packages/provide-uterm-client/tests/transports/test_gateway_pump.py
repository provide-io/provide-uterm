#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway — pump helpers (tcp↔ws) and TelnetWsGateway."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets
import websockets.server
from provide.terminal.control_channel import (
    ControlChannelDecoder,
    ControlChunk,
    encode_control,
)
from provide.terminal.gateway import (
    TelnetWsGateway,
    _normalize_crlf,
    _pipe_ws,
    _tcp_to_ws,
    _ws_to_tcp,
)

# ---------------------------------------------------------------------------
# Pump helper unit tests
# ---------------------------------------------------------------------------


def _decode_control(raw: str) -> dict[str, Any]:
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    assert len(events) == 1
    assert isinstance(events[0], ControlChunk)
    return events[0].control


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

        await _ws_to_tcp(_async_iter(["world"]), cast("StreamWriter", MockWriter()), token_holder=[None])
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

        await _ws_to_tcp(_async_iter([b"\xff\xfe"]), cast("StreamWriter", MockWriter()), token_holder=[None])
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
            # Disable IAC negotiation — the bare echo server has no telnet
            # stack to absorb DO TTYPE / NEW-ENVIRON, which would arrive
            # before the banner and crowd the read window.
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}", iac_negotiate=False)
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server

            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            data = b""
            deadline = asyncio.get_event_loop().time() + 2.0
            while banner not in data and asyncio.get_event_loop().time() < deadline:
                chunk = await asyncio.wait_for(reader.read(256), timeout=1.0)
                if not chunk:
                    break
                data += chunk
            writer.close()
            tcp_srv.close()
        finally:
            ws_srv.close()

        assert banner.decode("utf-8") in data.decode("utf-8")

    async def test_client_data_forwarded_to_ws(self) -> None:
        """Data sent by the telnet client is echoed back via the WS server."""
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}", iac_negotiate=False)
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server

            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(b"ping")
            await writer.drain()
            data = b""
            deadline = asyncio.get_event_loop().time() + 2.0
            while b"ping" not in data and asyncio.get_event_loop().time() < deadline:
                chunk = await asyncio.wait_for(reader.read(256), timeout=1.0)
                if not chunk:
                    break
                data += chunk
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
                _pipe_ws(
                    reader,
                    cast("StreamWriter", MockWriter()),
                    f"ws://127.0.0.1:{ws_port}",
                    token_holder=[None],
                ),
                timeout=3.0,
            )
        finally:
            ws_srv.close()


_PIPE_WS_PATH = "provide.terminal.gateway._telnet_gateway._pipe_ws"


class TestTelnetWsGatewayHandleException:
    async def test_handle_exception_closes_writer(self) -> None:
        """_handle closes writer even when _pipe_ws raises."""
        gw = TelnetWsGateway("ws://localhost")
        reader = MagicMock()
        # at_eof: False on entry (so pipe is called), True after (TCP closed)
        reader.at_eof.side_effect = [False, True]
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch(_PIPE_WS_PATH, side_effect=RuntimeError("boom")):
            await gw._handle(reader, writer)

        writer.close.assert_called()

    async def test_handle_reconnects_when_ws_closes_while_tcp_alive(self) -> None:
        """_handle retries _pipe_ws when WS closes but TCP client is still connected."""
        gw = TelnetWsGateway("ws://localhost")
        reader = MagicMock()
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        call_count = 0

        async def _fake_pipe(*_args: object, **_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1

        # at_eof: False on first 3 checks (before/after each pipe attempt),
        # True on 4th (TCP client finally closes after second WS drop)
        reader.at_eof.side_effect = [False, False, False, True]

        with (
            patch(_PIPE_WS_PATH, side_effect=_fake_pipe),
            patch("provide.terminal.gateway._telnet_gateway.asyncio.sleep", new_callable=AsyncMock),
        ):
            await gw._handle(reader, writer)

        assert call_count == 2, f"Expected 2 pipe attempts (reconnect once), got {call_count}"
        writer.close.assert_called()

    async def test_handle_no_retry_when_tcp_closes_first(self) -> None:
        """_handle does NOT retry when TCP client closes (reader at EOF)."""
        gw = TelnetWsGateway("ws://localhost")
        reader = MagicMock()
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        call_count = 0

        async def _fake_pipe(*_args: object, **_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1

        # at_eof: False before first call, True after (TCP closed after pipe)
        reader.at_eof.side_effect = [False, True]

        with patch(_PIPE_WS_PATH, side_effect=_fake_pipe):
            await gw._handle(reader, writer)

        assert call_count == 1, f"Expected exactly 1 pipe attempt (no retry), got {call_count}"
        writer.close.assert_called()


# ---------------------------------------------------------------------------
# _ws_to_tcp — resume/control integration
# ---------------------------------------------------------------------------


class TestWsToTcpResume:
    async def test_session_token_intercepted_not_forwarded(self) -> None:
        token_holder: list[dict | None] = [None]
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        msg = encode_control({"type": "session_token", "token": "tok123"})
        await _ws_to_tcp(_async_iter([msg]), cast("StreamWriter", MockWriter()), token_holder=token_holder)
        assert written == []
        assert token_holder[0] is not None
        assert token_holder[0]["token"] == "tok123"

    async def test_resume_ok_sends_text_to_tcp(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(
            _async_iter([encode_control({"type": "resume_ok"})]),
            cast("StreamWriter", MockWriter()),
            token_holder=[None],
        )
        assert any(b"Session resumed" in w for w in written)

    async def test_resume_failed_clears_token_holder(self) -> None:
        token_holder: list[dict | None] = [{"token": "old"}]
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(
            _async_iter([encode_control({"type": "resume_failed"})]),
            cast("StreamWriter", MockWriter()),
            token_holder=token_holder,
        )
        assert token_holder[0] is None

    async def test_plain_text_forwarded(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["hello"]), cast("StreamWriter", MockWriter()), token_holder=[None])
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

        await _ws_to_tcp(_async_iter(["foo\nbar"]), cast("StreamWriter", MockWriter()), token_holder=[None])
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

        await _ws_to_tcp(_async_iter(["foo\r\nbar"]), cast("StreamWriter", MockWriter()), token_holder=[None])
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
    async def test_token_holder_present_sends_resume(self, tmp_path) -> None:
        """When a token_holder contains a token dict, the first WS message should be an encoded resume control frame."""
        received: list[str] = []
        first_message = asyncio.Event()

        async def handler(ws: websockets.ServerConnection) -> None:
            try:
                async for msg in ws:
                    received.append(msg if isinstance(msg, str) else msg.decode())
                    first_message.set()
            except websockets.exceptions.ConnectionClosed:
                pass

        srv = await websockets.serve(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            token_holder: list[dict | None] = [{"token": "resume_tok_abc"}]

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
                _pipe_ws(
                    reader,
                    cast("StreamWriter", MockWriter()),
                    f"ws://127.0.0.1:{port}",
                    token_holder=token_holder,
                ),
                timeout=3.0,
            )
            # Wait for the server to actually process the first frame — the
            # client closes its side as soon as the EOF reader task wins, so
            # the server-side handler may still be draining.
            await asyncio.wait_for(first_message.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        assert len(received) >= 1
        first = _decode_control(received[0])
        assert first == {"type": "resume", "token": "resume_tok_abc"}

    async def test_no_token_holder_sends_no_resume(self) -> None:
        """When token_holder is [None], no resume message is sent."""
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
                _pipe_ws(
                    reader,
                    cast("StreamWriter", MockWriter()),
                    f"ws://127.0.0.1:{port}",
                    token_holder=[None],
                ),
                timeout=3.0,
            )
        finally:
            srv.close()

        assert received == []
