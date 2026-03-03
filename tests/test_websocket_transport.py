#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for WebSocketStreamReader and WebSocketStreamWriter.

Uses a mock WebSocket object — no full FastAPI server needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from provide.terminal.transports.websocket import WebSocketStreamReader, WebSocketStreamWriter


def _make_ws(host: str = "127.0.0.1", port: int = 9999) -> MagicMock:
    ws = MagicMock()
    ws.client = MagicMock()
    ws.client.host = host
    ws.client.port = port
    ws.receive_text = AsyncMock()
    ws.send_text = AsyncMock()
    return ws


class TestWebSocketStreamReader:
    async def test_read_single_message(self) -> None:
        ws = _make_ws()
        ws.receive_text.return_value = "AB"
        reader = WebSocketStreamReader(ws)
        data = await reader.read(2)
        assert data == b"AB"

    async def test_read_partial_then_buffered(self) -> None:
        ws = _make_ws()
        ws.receive_text.return_value = "ABCD"
        reader = WebSocketStreamReader(ws)
        first = await reader.read(2)
        assert first == b"AB"
        # Second read should use buffered remainder without new WS message
        ws.receive_text.return_value = "XX"
        second = await reader.read(2)
        assert second == b"CD"

    async def test_disconnect_returns_empty(self) -> None:
        from fastapi import WebSocketDisconnect

        ws = _make_ws()
        ws.receive_text.side_effect = WebSocketDisconnect()
        reader = WebSocketStreamReader(ws)
        data = await reader.read(1)
        assert data == b""

    async def test_read_non_ascii_utf8_roundtrip(self) -> None:
        """Non-ASCII text from the browser must survive the encode step intact."""
        ws = _make_ws()
        text = "→ café 🎮"
        encoded = text.encode("utf-8")
        ws.receive_text.return_value = text
        reader = WebSocketStreamReader(ws)
        data = await reader.read(len(encoded))
        assert data == encoded

    async def test_closed_returns_empty_immediately(self) -> None:
        ws = _make_ws()
        reader = WebSocketStreamReader(ws)
        reader._closed = True
        data = await reader.read(1)
        assert data == b""
        ws.receive_text.assert_not_called()


class TestWebSocketStreamWriter:
    async def test_write_and_drain(self) -> None:
        ws = _make_ws()
        writer = WebSocketStreamWriter(ws)
        writer.write(b"hello")
        await writer.drain()
        ws.send_text.assert_called_once_with("hello")

    async def test_drain_empty_buffer_no_send(self) -> None:
        ws = _make_ws()
        writer = WebSocketStreamWriter(ws)
        await writer.drain()
        ws.send_text.assert_not_called()

    async def test_close_prevents_write(self) -> None:
        ws = _make_ws()
        writer = WebSocketStreamWriter(ws)
        writer.close()
        writer.write(b"nope")
        await writer.drain()
        ws.send_text.assert_not_called()

    def test_get_extra_info_peername(self) -> None:
        ws = _make_ws(host="10.0.0.1", port=1234)
        writer = WebSocketStreamWriter(ws)
        peername = writer.get_extra_info("peername")
        assert peername == ("10.0.0.1", 1234)

    def test_get_extra_info_unknown_key(self) -> None:
        ws = _make_ws()
        writer = WebSocketStreamWriter(ws)
        assert writer.get_extra_info("something", default="x") == "x"

    async def test_wait_closed_noop(self) -> None:
        ws = _make_ws()
        writer = WebSocketStreamWriter(ws)
        await writer.wait_closed()  # should not raise

    async def test_drain_with_disconnect_marks_closed(self) -> None:
        from fastapi import WebSocketDisconnect
        from provide.terminal.transports.websocket import WebSocketStreamWriter

        class _DisconnectingWs:
            async def send_text(self, text: str) -> None:
                raise WebSocketDisconnect(code=1000)

        writer = WebSocketStreamWriter(_DisconnectingWs())
        writer.write(b"data")
        await writer.drain()
        assert writer._closed
