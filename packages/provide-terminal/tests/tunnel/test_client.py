#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the async WebSocket tunnel client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.asyncio.server import serve

from provide.terminal.tunnel.client import (
    BACKOFF_SCHEDULE,
    TunnelClient,
)
from provide.terminal.tunnel.protocol import (
    CHANNEL_CONTROL,
    CHANNEL_DATA,
    FLAG_DATA,
    TunnelFrame,
    decode_control,
    decode_frame,
    encode_control,
    encode_frame,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_ws(*, state_name: str = "OPEN") -> MagicMock:
    """Create a mock ClientConnection."""
    ws = AsyncMock()
    ws.protocol = MagicMock()
    ws.protocol.state.name = state_name
    ws.close = AsyncMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# Unit tests (mocked WebSocket)
# ---------------------------------------------------------------------------


class TestConnectedProperty:
    def test_not_connected_initially(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        assert not client.connected

    def test_connected_when_open(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        client._ws = _mock_ws(state_name="OPEN")
        assert client.connected

    def test_not_connected_when_closed(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        client._ws = _mock_ws(state_name="CLOSED")
        assert not client.connected

    def test_not_connected_when_closing(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        client._ws = _mock_ws(state_name="CLOSING")
        assert not client.connected

    def test_not_connected_when_protocol_raises(self) -> None:
        """connected returns False when accessing protocol.state raises."""
        client = TunnelClient("ws://localhost:9999", "tok")

        class _BrokenWs:
            @property
            def protocol(self):
                raise AttributeError("gone")

        client._ws = _BrokenWs()
        assert not client.connected


class TestConnect:
    async def test_connect_sets_ws(self) -> None:
        client = TunnelClient("ws://localhost:9999", "mytoken")
        mock_ws = _mock_ws()

        async def fake_connect(*a: object, **kw: object) -> object:
            return mock_ws

        with patch("provide.terminal.tunnel.client.connect", side_effect=fake_connect):
            await client.connect()
        assert client._ws is mock_ws

    async def test_connect_passes_auth_header(self) -> None:
        client = TunnelClient("ws://localhost:9999", "secret")

        async def fake_connect(*a: object, **kw: object) -> object:
            return _mock_ws()

        with patch("provide.terminal.tunnel.client.connect", side_effect=fake_connect) as mock_connect:
            await client.connect()
            mock_connect.assert_called_once_with(
                "ws://localhost:9999",
                additional_headers={"Authorization": "Bearer secret"},
            )


class TestClose:
    async def test_close_when_connected(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        client._ws = mock_ws
        await client.close()
        mock_ws.close.assert_awaited_once()
        assert client._ws is None

    async def test_close_idempotent(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        await client.close()  # no error when not connected
        await client.close()  # still no error
        assert client._ws is None

    async def test_close_clears_ws_before_closing(self) -> None:
        """Ensure _ws is set to None even if ws.close() raises."""
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        mock_ws.close = AsyncMock(side_effect=RuntimeError("boom"))
        client._ws = mock_ws
        with pytest.raises(RuntimeError, match="boom"):
            await client.close()
        assert client._ws is None


class TestSendData:
    async def test_send_data_encodes_frame(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        client._ws = mock_ws
        await client.send_data(b"hello")
        expected = encode_frame(CHANNEL_DATA, b"hello")
        mock_ws.send.assert_awaited_once_with(expected)

    async def test_send_data_custom_channel(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        client._ws = mock_ws
        await client.send_data(b"err", channel=0x02)
        expected = encode_frame(0x02, b"err")
        mock_ws.send.assert_awaited_once_with(expected)

    async def test_send_data_raises_when_not_connected(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        with pytest.raises(RuntimeError, match="not connected"):
            await client.send_data(b"x")


class TestOpenTerminal:
    async def test_sends_correct_control_message(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        client._ws = mock_ws
        await client.open_terminal(cols=120, rows=40)
        raw = mock_ws.send.call_args[0][0]
        frame = decode_frame(raw)
        assert frame.is_control
        msg = decode_control(frame.payload)
        assert msg == {
            "type": "open",
            "channel": 1,
            "tunnel_type": "terminal",
            "term_size": [120, 40],
        }


class TestSendResize:
    async def test_sends_correct_control_message(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        client._ws = mock_ws
        await client.send_resize(cols=200, rows=50)
        raw = mock_ws.send.call_args[0][0]
        frame = decode_frame(raw)
        assert frame.is_control
        msg = decode_control(frame.payload)
        assert msg == {"type": "resize", "channel": 1, "cols": 200, "rows": 50}


class TestSendEof:
    async def test_sends_eof_flag(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        client._ws = mock_ws
        await client.send_eof()
        raw = mock_ws.send.call_args[0][0]
        frame = decode_frame(raw)
        assert frame.channel == CHANNEL_DATA
        assert frame.is_eof
        assert frame.payload == b""

    async def test_sends_eof_custom_channel(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        client._ws = mock_ws
        await client.send_eof(channel=0x02)
        raw = mock_ws.send.call_args[0][0]
        frame = decode_frame(raw)
        assert frame.channel == 0x02
        assert frame.is_eof


class TestRecv:
    async def test_recv_decodes_binary_frame(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        mock_ws.recv = AsyncMock(return_value=encode_frame(CHANNEL_DATA, b"world"))
        client._ws = mock_ws
        frame = await client.recv()
        assert frame == TunnelFrame(channel=CHANNEL_DATA, flags=FLAG_DATA, payload=b"world")

    async def test_recv_handles_string_message(self) -> None:
        """If the server sends text, recv should still decode it."""
        client = TunnelClient("ws://localhost:9999", "tok")
        mock_ws = _mock_ws()
        raw = encode_frame(CHANNEL_CONTROL, b'{"type":"ack"}')
        # Simulate receiving as str (unlikely but handled)
        mock_ws.recv = AsyncMock(return_value=raw.decode("latin-1"))
        client._ws = mock_ws
        frame = await client.recv()
        assert frame.is_control

    async def test_recv_raises_when_not_connected(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        with pytest.raises(RuntimeError, match="not connected"):
            await client.recv()


class TestReconnectLoop:
    async def test_reconnect_succeeds_on_first_attempt(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        with (
            patch.object(client, "connect", new_callable=AsyncMock) as mock_connect,
            patch("provide.terminal.tunnel.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await client.reconnect_loop(max_attempts=3)
        mock_connect.assert_awaited_once()
        mock_sleep.assert_awaited_once_with(BACKOFF_SCHEDULE[0])

    async def test_reconnect_retries_on_failure(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        call_count = 0

        async def fail_then_succeed() -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionRefusedError("nope")

        with (
            patch.object(client, "connect", side_effect=fail_then_succeed),
            patch("provide.terminal.tunnel.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await client.reconnect_loop(max_attempts=5)
        assert call_count == 3
        # Delays: 1, 2, 5 (indices 0, 1, 2 of BACKOFF_SCHEDULE)
        delays = [c.args[0] for c in mock_sleep.await_args_list]
        assert delays == [1, 2, 5]

    async def test_reconnect_gives_up_after_max_attempts(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        with (
            patch.object(client, "connect", side_effect=ConnectionRefusedError("nope")),
            patch("provide.terminal.tunnel.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            await client.reconnect_loop(max_attempts=2)
        # Should not raise, just log and return
        assert not client.connected

    async def test_reconnect_backoff_caps_at_last_value(self) -> None:
        client = TunnelClient("ws://localhost:9999", "tok")
        with (
            patch.object(client, "connect", side_effect=ConnectionRefusedError("nope")),
            patch("provide.terminal.tunnel.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await client.reconnect_loop(max_attempts=7)
        delays = [c.args[0] for c in mock_sleep.await_args_list]
        assert delays == [1, 2, 5, 10, 30, 30, 30]


# ---------------------------------------------------------------------------
# Integration tests (real in-process WebSocket server)
# ---------------------------------------------------------------------------


class TestIntegration:
    """Spin up a real websockets server and exercise the client end-to-end."""

    async def test_roundtrip_data(self) -> None:
        """Client sends data, server echoes it back."""
        received: list[bytes] = []

        async def handler(ws: object) -> None:
            # websockets 16 handler signature
            msg = await ws.recv()  # type: ignore[union-attr]
            received.append(msg)
            await ws.send(msg)  # type: ignore[union-attr]

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = TunnelClient(f"ws://127.0.0.1:{port}", "tok")
            # Patch out auth header check since our test server doesn't care
            await client.connect()
            try:
                await client.send_data(b"ping")
                frame = await client.recv()
                assert frame.channel == CHANNEL_DATA
                assert frame.payload == b"ping"
            finally:
                await client.close()

        assert not client.connected

    async def test_open_terminal_and_resize(self) -> None:
        """Server receives open + resize control messages."""
        messages: list[dict] = []

        async def handler(ws: object) -> None:
            for _ in range(2):
                raw = await ws.recv()  # type: ignore[union-attr]
                frame = decode_frame(raw)
                messages.append(decode_control(frame.payload))
            # Send an ack back so client can recv
            await ws.send(encode_control({"type": "ack"}))  # type: ignore[union-attr]

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = TunnelClient(f"ws://127.0.0.1:{port}", "tok")
            await client.connect()
            try:
                await client.open_terminal(80, 24)
                await client.send_resize(120, 40)
                ack = await client.recv()
                assert decode_control(ack.payload)["type"] == "ack"
            finally:
                await client.close()

        assert messages[0]["type"] == "open"
        assert messages[0]["term_size"] == [80, 24]
        assert messages[1]["type"] == "resize"
        assert messages[1]["cols"] == 120

    async def test_eof_frame(self) -> None:
        """Server receives an EOF frame."""
        frames: list[TunnelFrame] = []

        async def handler(ws: object) -> None:
            raw = await ws.recv()  # type: ignore[union-attr]
            frames.append(decode_frame(raw))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = TunnelClient(f"ws://127.0.0.1:{port}", "tok")
            await client.connect()
            try:
                await client.send_eof()
            finally:
                await client.close()

        assert len(frames) == 1
        assert frames[0].is_eof
