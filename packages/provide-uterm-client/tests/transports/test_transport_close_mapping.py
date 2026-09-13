#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Every transport reports the end of a connection as a typed close.

websockets knows which side sent the close frame, its code and its reason; a
telnet socket knows whether the peer sent EOF, reset the connection, or whether
we gave up on it. These tests pin that each transport passes that on as a
``TransportClosedError`` instead of flattening it into a message string.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from provide.uterm.transport_close import CloseInitiator, TransportClosedError
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close
from websockets.protocol import State

from provide.uterm.transports.chaos import ChaosTransport
from provide.uterm.transports.telnet import TelnetTransport
from provide.uterm.transports.telnet_transport import _MAX_RX_BUF_BYTES
from provide.uterm.transports.ws_transport import WebSocketTransport


def _ws() -> MagicMock:
    ws = MagicMock(spec=["send", "recv", "close", "wait_closed", "state"])
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    ws.wait_closed = AsyncMock()
    ws.state = State.OPEN
    return ws


async def _connected_ws(monkeypatch: pytest.MonkeyPatch, ws: MagicMock) -> WebSocketTransport:
    monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
    transport = WebSocketTransport()
    await transport.connect("h", 1)
    return transport


_WS_CLOSES = [
    pytest.param(
        None,
        Close(1011, "keepalive ping timeout"),
        None,
        CloseInitiator.LOCAL,
        1011,
        "keepalive ping timeout",
        id="our-ping-timeout",
    ),
    pytest.param(Close(1001, "going away"), None, None, CloseInitiator.REMOTE, 1001, "going away", id="peer-closed"),
    pytest.param(Close(1000, ""), Close(1000, ""), True, CloseInitiator.REMOTE, 1000, "", id="peer-first-handshake"),
    pytest.param(
        Close(1000, ""), Close(1000, "bye"), False, CloseInitiator.LOCAL, 1000, "bye", id="we-first-handshake"
    ),
    pytest.param(None, None, None, CloseInitiator.UNKNOWN, None, "", id="no-close-frames"),
]


@pytest.mark.parametrize(("rcvd", "sent", "rcvd_then_sent", "initiator", "code", "reason"), _WS_CLOSES)
async def test_websocket_receive_reports_who_closed(
    monkeypatch: pytest.MonkeyPatch,
    rcvd: Close | None,
    sent: Close | None,
    rcvd_then_sent: bool | None,
    initiator: CloseInitiator,
    code: int | None,
    reason: str,
) -> None:
    ws = _ws()
    ws.recv = AsyncMock(side_effect=ConnectionClosed(rcvd, sent, rcvd_then_sent))
    transport = await _connected_ws(monkeypatch, ws)

    with pytest.raises(TransportClosedError, match="^Connection closed") as caught:
        await transport.receive(4096, 100)

    assert (caught.value.close.initiator, caught.value.close.code, caught.value.close.reason) == (
        initiator,
        code,
        reason,
    )
    assert transport.is_connected() is False


async def test_websocket_send_reports_who_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _ws()
    ws.send = AsyncMock(side_effect=ConnectionClosed(None, Close(1011, "keepalive ping timeout")))
    transport = await _connected_ws(monkeypatch, ws)

    with pytest.raises(TransportClosedError, match="^Connection closed") as caught:
        await transport.send(b"hello")

    assert caught.value.close.initiator is CloseInitiator.LOCAL
    assert caught.value.close.code == 1011


async def test_websocket_receive_failure_is_an_unknown_close(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _ws()
    ws.recv = AsyncMock(side_effect=RuntimeError("kaboom"))
    transport = await _connected_ws(monkeypatch, ws)

    with pytest.raises(TransportClosedError, match="^WebSocket receive error") as caught:
        await transport.receive(4096, 100)

    assert caught.value.close.initiator is CloseInitiator.UNKNOWN
    assert caught.value.close.detail == "RuntimeError: kaboom"


def _telnet(*, read: AsyncMock | None = None, drain: AsyncMock | None = None) -> TelnetTransport:
    transport = TelnetTransport()
    writer = MagicMock()
    writer.is_closing = MagicMock(return_value=False)
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.write = MagicMock()
    writer.drain = drain or AsyncMock()
    reader = MagicMock()
    reader.read = read or AsyncMock(return_value=b"")
    transport._writer = writer
    transport._reader = reader
    return transport


async def test_telnet_eof_is_a_remote_close() -> None:
    with pytest.raises(TransportClosedError, match="^Connection closed by remote") as caught:
        await _telnet(read=AsyncMock(return_value=b"")).receive(64, 1000)

    assert caught.value.close.initiator is CloseInitiator.REMOTE


@pytest.mark.parametrize(
    ("error", "initiator"),
    [(ConnectionResetError("peer reset"), CloseInitiator.REMOTE), (BrokenPipeError("epipe"), CloseInitiator.UNKNOWN)],
)
async def test_telnet_receive_loss_says_how(error: OSError, initiator: CloseInitiator) -> None:
    with pytest.raises(TransportClosedError, match="^Connection lost") as caught:
        await _telnet(read=AsyncMock(side_effect=error)).receive(64, 1000)

    assert caught.value.close.initiator is initiator
    assert caught.value.close.detail == f"{type(error).__name__}: {error}"


@pytest.mark.parametrize(
    ("error", "initiator"),
    [(ConnectionResetError("peer reset"), CloseInitiator.REMOTE), (BrokenPipeError("epipe"), CloseInitiator.UNKNOWN)],
)
async def test_telnet_send_loss_says_how(error: OSError, initiator: CloseInitiator) -> None:
    with pytest.raises(TransportClosedError, match="^Send failed") as caught:
        await _telnet(drain=AsyncMock(side_effect=error)).send(b"hello")

    assert caught.value.close.initiator is initiator


async def test_telnet_buffer_overflow_is_a_local_close() -> None:
    unterminated_subnegotiation = b"\xff\xfa" + b"a" * (_MAX_RX_BUF_BYTES + 1)

    with pytest.raises(TransportClosedError, match="^telnet receive buffer exceeded") as caught:
        await _telnet(read=AsyncMock(return_value=unterminated_subnegotiation)).receive(_MAX_RX_BUF_BYTES * 2, 1000)

    assert caught.value.close.initiator is CloseInitiator.LOCAL


async def test_not_connected_is_not_a_close() -> None:
    with pytest.raises(ConnectionError, match="Not connected") as caught:
        await TelnetTransport().receive(64, 1000)

    assert not isinstance(caught.value, TransportClosedError)


async def test_an_injected_chaos_disconnect_is_a_typed_close() -> None:
    inner = MagicMock()
    inner.disconnect = AsyncMock()
    chaos = ChaosTransport(inner, disconnect_every_n_receives=1, label="chaos")

    with pytest.raises(TransportClosedError, match="^chaos: injected disconnect on receive #1") as caught:
        await chaos.receive(64, 100)

    assert caught.value.close.initiator is CloseInitiator.UNKNOWN
