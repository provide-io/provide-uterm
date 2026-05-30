#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Test that TelnetSessionConnector.poll_messages handles a capped-buffer
ConnectionError from the transport gracefully (DoS hardening).

Test 3: receive() raises ConnectionError → poll_messages returns [], sets
_connected=False, calls disconnect().
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from provide.uterm.server.connectors.telnet import TelnetSessionConnector


def _make_transport(*, raise_on_receive: Exception | None = None, connected: bool = True) -> MagicMock:
    t = MagicMock()
    t.connect = AsyncMock()
    t.disconnect = AsyncMock()
    t.send = AsyncMock()
    if raise_on_receive is not None:
        t.receive = AsyncMock(side_effect=raise_on_receive)
    else:
        t.receive = AsyncMock(return_value=b"")
    t.is_connected = MagicMock(return_value=connected)
    return t


def _make_connector(transport: MagicMock) -> TelnetSessionConnector:
    c = TelnetSessionConnector("s1", "Test", {"host": "127.0.0.1", "port": 2323})
    c._transport = transport
    c._connected = True
    return c


class TestPollMessagesConnectionError:
    async def test_connection_error_returns_empty_list(self) -> None:
        """poll_messages returns [] when receive() raises ConnectionError."""
        exc = ConnectionError("telnet receive buffer exceeded 262144 bytes (likely IAC SB without IAC SE)")
        t = _make_transport(raise_on_receive=exc)
        c = _make_connector(t)
        result = await c.poll_messages()
        assert result == []

    async def test_connection_error_sets_connected_false(self) -> None:
        """_connected must be False after a receive() ConnectionError."""
        exc = ConnectionError("telnet receive buffer exceeded 262144 bytes")
        t = _make_transport(raise_on_receive=exc)
        c = _make_connector(t)
        await c.poll_messages()
        assert c._connected is False

    async def test_connection_error_calls_disconnect(self) -> None:
        """disconnect() must be called on the transport after a ConnectionError."""
        exc = ConnectionError("telnet receive buffer exceeded 262144 bytes")
        t = _make_transport(raise_on_receive=exc)
        c = _make_connector(t)
        await c.poll_messages()
        t.disconnect.assert_awaited_once()

    async def test_disconnect_error_suppressed(self) -> None:
        """If disconnect() itself raises, poll_messages must still return [] cleanly."""
        exc = ConnectionError("cap exceeded")
        t = _make_transport(raise_on_receive=exc)
        t.disconnect = AsyncMock(side_effect=RuntimeError("already closed"))
        c = _make_connector(t)
        # Should not propagate the RuntimeError from disconnect
        result = await c.poll_messages()
        assert result == []
        assert c._connected is False
