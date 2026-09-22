#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Every transport reports the end of a connection as a typed close.

websockets knows which side sent the close frame, its code and its reason; a
telnet socket knows whether the peer sent EOF, reset the connection, or whether
we gave up on it. These tests pin that each transport passes that on as a
``TransportClosedError`` instead of flattening it into a message string.

The cases come from ``close_cases`` in ``spec/behavior_vectors.json``, the
fixture the TypeScript, Go and C# ports are tested against too, so this suite
is also the check that the Python reference agrees with every vector.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
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


def _close_cases() -> dict[str, Any]:
    here = Path(__file__).resolve()
    for path in (
        here.parents[4] / "spec" / "behavior_vectors.json",  # repo root
        here.parents[5] / "spec" / "behavior_vectors.json",  # mutants/<root>
    ):
        if path.is_file():
            cases: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["close_cases"]
            return cases
    raise FileNotFoundError("behavior_vectors.json not found")


CLOSE_CASES = _close_cases()


def _frame(value: dict[str, Any] | None) -> Close | None:
    return None if value is None else Close(value["code"], value["reason"])


def _websockets_close(case: dict[str, Any]) -> ConnectionClosed:
    return ConnectionClosed(_frame(case["received"]), _frame(case["sent"]), case["received_then_sent"])


def _assert_close(error: TransportClosedError, case: dict[str, Any]) -> None:
    close = error.close
    assert (close.initiator.value, close.code, close.reason) == (case["initiator"], case["code"], case["reason"])


@pytest.mark.parametrize("case", CLOSE_CASES["websocket"], ids=lambda case: case["name"])
async def test_websocket_receive_reports_who_closed(monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]) -> None:
    ws = _ws()
    ws.recv = AsyncMock(side_effect=_websockets_close(case))
    transport = await _connected_ws(monkeypatch, ws)

    with pytest.raises(TransportClosedError, match="^Connection closed") as caught:
        await transport.receive(4096, 100)

    _assert_close(caught.value, case)
    assert caught.value.close.detail == case["detail"]
    assert transport.is_connected() is False


@pytest.mark.parametrize("case", CLOSE_CASES["websocket"], ids=lambda case: case["name"])
async def test_websocket_send_reports_who_closed(monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]) -> None:
    ws = _ws()
    ws.send = AsyncMock(side_effect=_websockets_close(case))
    transport = await _connected_ws(monkeypatch, ws)

    with pytest.raises(TransportClosedError, match="^Connection closed") as caught:
        await transport.send(b"hello")

    _assert_close(caught.value, case)
    assert caught.value.close.detail == case["detail"]


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


def _socket_error(event: str) -> OSError:
    return ConnectionResetError("peer reset") if event == "reset" else BrokenPipeError("epipe")


# The message prefix is the transport's own wording; the close is the shared contract.
_TELNET_PREFIXES = {
    ("eof", "receive"): "^Connection closed by remote",
    ("reset", "receive"): "^Connection lost",
    ("broken_pipe", "receive"): "^Connection lost",
    ("reset", "send"): "^Send failed",
    ("broken_pipe", "send"): "^Send failed",
    ("rx_buffer_cap", "receive"): "^telnet receive buffer exceeded",
}


async def _drive_telnet(event: str, operation: str) -> None:
    if event == "eof":
        await _telnet(read=AsyncMock(return_value=b"")).receive(64, 1000)
    elif event == "rx_buffer_cap":
        unterminated_subnegotiation = b"\xff\xfa" + b"a" * (_MAX_RX_BUF_BYTES + 1)
        transport = _telnet(read=AsyncMock(return_value=unterminated_subnegotiation))
        await transport.receive(_MAX_RX_BUF_BYTES * 2, 1000)
    elif operation == "receive":
        await _telnet(read=AsyncMock(side_effect=_socket_error(event))).receive(64, 1000)
    else:
        await _telnet(drain=AsyncMock(side_effect=_socket_error(event))).send(b"hello")


@pytest.mark.parametrize("case", CLOSE_CASES["telnet"], ids=lambda case: case["name"])
async def test_telnet_reports_who_closed(case: dict[str, Any]) -> None:
    with pytest.raises(TransportClosedError, match=_TELNET_PREFIXES[case["event"], case["operation"]]) as caught:
        await _drive_telnet(case["event"], case["operation"])

    _assert_close(caught.value, case)


@pytest.mark.parametrize("error", [ConnectionResetError("peer reset"), BrokenPipeError("epipe")])
async def test_telnet_loss_names_the_socket_error(error: OSError) -> None:
    with pytest.raises(TransportClosedError, match="^Connection lost") as caught:
        await _telnet(read=AsyncMock(side_effect=error)).receive(64, 1000)

    assert caught.value.close.detail == f"{type(error).__name__}: {error}"


async def test_not_connected_is_not_a_close() -> None:
    with pytest.raises(ConnectionError, match="Not connected") as caught:
        await TelnetTransport().receive(64, 1000)

    assert not isinstance(caught.value, TransportClosedError)


@pytest.mark.parametrize("case", CLOSE_CASES["chaos"], ids=lambda case: case["name"])
async def test_an_injected_chaos_disconnect_is_a_typed_close(case: dict[str, Any]) -> None:
    inner = MagicMock()
    inner.disconnect = AsyncMock()
    chaos = ChaosTransport(inner, disconnect_every_n_receives=1, label="chaos")

    with pytest.raises(TransportClosedError, match="^chaos: injected disconnect on receive #1") as caught:
        await chaos.receive(64, 100)

    _assert_close(caught.value, case)
