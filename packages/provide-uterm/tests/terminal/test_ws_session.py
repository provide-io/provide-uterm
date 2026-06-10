#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for WebSocketSession with a mocked WebSocketTransport."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.transport_session import TransportSession
from provide.uterm.ws_session import WebSocketSession, connect_ws

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_transport() -> MagicMock:
    transport = MagicMock()
    transport.connect = AsyncMock()
    transport.disconnect = AsyncMock()
    transport.send = AsyncMock()
    transport.receive = AsyncMock(side_effect=[b"hello", b"", b"", b""])
    return transport


# ---------------------------------------------------------------------------
# Constructor / inheritance
# ---------------------------------------------------------------------------


def test_is_transport_session_subclass() -> None:
    assert issubclass(WebSocketSession, TransportSession)


def test_constructor_defaults() -> None:
    session = WebSocketSession("wss://example.com/ws")
    assert session.url == "wss://example.com/ws"
    assert session._cols == 80
    assert session._rows == 25
    assert session._send_encoding == "utf-8"
    assert not session.is_connected()


def test_constructor_custom_params() -> None:
    session = WebSocketSession("ws://h/ws", cols=120, rows=40)
    assert session._cols == 120
    assert session._rows == 40


# ---------------------------------------------------------------------------
# _connect_transport hook
# ---------------------------------------------------------------------------


async def test_connect_transport_args() -> None:
    session = WebSocketSession("wss://example.com/ws")
    transport = _mock_transport()
    session._transport = transport

    await session._connect_transport()

    transport.connect.assert_awaited_once_with(
        host="", port=0, url="wss://example.com/ws", ping_interval=20, ping_timeout=20, close_timeout=10
    )


async def test_connect_transport_threads_close_timeout() -> None:
    session = WebSocketSession("wss://example.com/ws", close_timeout=7)
    transport = _mock_transport()
    session._transport = transport

    await session._connect_transport()

    transport.connect.assert_awaited_once_with(
        host="", port=0, url="wss://example.com/ws", ping_interval=20, ping_timeout=20, close_timeout=7
    )


async def test_connect_starts_reader() -> None:
    session = WebSocketSession("wss://example.com/ws")
    transport = _mock_transport()
    session._transport = transport

    await session.connect()
    assert session.is_connected()
    assert session._read_task is not None
    transport.connect.assert_awaited_once_with(
        host="", port=0, url="wss://example.com/ws", ping_interval=20, ping_timeout=20, close_timeout=10
    )

    await session.close()
    assert not session.is_connected()
    transport.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# connect_ws factory
# ---------------------------------------------------------------------------


async def test_connect_ws_factory() -> None:
    with patch("provide.uterm.ws_session.WebSocketTransport") as mock_cls:
        mock_t = _mock_transport()
        mock_cls.return_value = mock_t

        session = await connect_ws("wss://bbs.example.com/ws", cols=100, rows=30)

        assert isinstance(session, WebSocketSession)
        assert session.is_connected()
        assert session._cols == 100
        assert session._rows == 30
        mock_t.connect.assert_awaited_once_with(
            host="", port=0, url="wss://bbs.example.com/ws", ping_interval=20, ping_timeout=20, close_timeout=10
        )

        await session.close()


# ---------------------------------------------------------------------------
# send encodes utf-8
# ---------------------------------------------------------------------------


async def test_send_encodes_utf8() -> None:
    session = WebSocketSession("wss://h/ws")
    transport = _mock_transport()
    session._transport = transport
    await session.send("héllo")
    transport.send.assert_awaited_once_with("héllo".encode())


# ---------------------------------------------------------------------------
# end-to-end against a fake transport: Session protocol methods work
# ---------------------------------------------------------------------------


async def test_session_methods_end_to_end() -> None:
    with patch("provide.uterm.ws_session.WebSocketTransport") as mock_cls:
        mock_t = _mock_transport()
        mock_t.receive = AsyncMock(side_effect=[b"line-1", b"line-2", ConnectionResetError("done")])
        mock_cls.return_value = mock_t

        session = await connect_ws("wss://h/ws")
        await asyncio.sleep(0.1)

        # Inherited Session protocol surface works end-to-end.
        assert session.screen_change_seq() >= 1
        snap = session.snapshot()
        assert "line-2" in snap["screen"]
        assert isinstance(session.ansi_screen(), str)
        assert await session.wait_for_update(timeout_ms=10) is False

        await session.close()
        assert not session.is_connected()
