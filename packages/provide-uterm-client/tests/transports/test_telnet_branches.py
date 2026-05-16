#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TelnetTransport._negotiate() branch coverage — all negotiation
branches exercised via receive(), plus deduplication and no-writer guard tests."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from provide.uterm.transports.telnet import IAC, TelnetTransport


async def _make_server_that_sends(data: bytes) -> tuple[asyncio.Server, int]:
    """Start a bare TCP server that sends *data* immediately after connect."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(data)
        await writer.drain()
        await asyncio.sleep(2)

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


# ---------------------------------------------------------------------------
# TelnetTransport._negotiate() — all branches via receive()
# ---------------------------------------------------------------------------


class TestTelnetNegotiateBranches:
    async def _negotiate_with(self, cmd_bytes: bytes) -> None:
        """Connect, have server send *cmd_bytes*, then call receive() to trigger negotiate."""
        server, port = await _make_server_that_sends(cmd_bytes)
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            with contextlib.suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(t.receive(256, 200), timeout=1.0)
            # Give background tasks a moment to complete
            await asyncio.sleep(0.05)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_negotiate_dont_branch(self) -> None:
        """DONT branch records in negotiated['dont'] and calls _send_wont."""
        from provide.uterm.transports.telnet import DONT

        await self._negotiate_with(bytes([IAC, DONT, 1]))

    async def test_negotiate_will_branch_known(self) -> None:
        """WILL ECHO branch calls _send_do."""
        from provide.uterm.transports.telnet import OPT_ECHO, WILL

        await self._negotiate_with(bytes([IAC, WILL, OPT_ECHO]))

    async def test_negotiate_will_branch_unknown(self) -> None:
        """WILL <unknown> branch calls _send_dont."""
        from provide.uterm.transports.telnet import WILL

        await self._negotiate_with(bytes([IAC, WILL, 77]))

    async def test_negotiate_wont_branch(self) -> None:
        """WONT branch calls _send_dont."""
        from provide.uterm.transports.telnet import WONT

        await self._negotiate_with(bytes([IAC, WONT, 1]))

    async def test_negotiate_do_binary_branch(self) -> None:
        """DO BINARY branch calls _send_will."""
        from provide.uterm.transports.telnet import DO, OPT_BINARY

        await self._negotiate_with(bytes([IAC, DO, OPT_BINARY]))

    async def test_negotiate_do_naws_branch(self) -> None:
        """DO NAWS branch calls _send_will + _send_naws."""
        from provide.uterm.transports.telnet import DO, OPT_NAWS

        await self._negotiate_with(bytes([IAC, DO, OPT_NAWS]))

    async def test_negotiate_do_ttype_branch(self) -> None:
        """DO TTYPE branch calls _send_will + _send_ttype."""
        from provide.uterm.transports.telnet import DO, OPT_TTYPE

        await self._negotiate_with(bytes([IAC, DO, OPT_TTYPE]))

    async def test_negotiate_do_unknown_branch(self) -> None:
        """DO <unknown> branch calls _send_wont."""
        from provide.uterm.transports.telnet import DO

        await self._negotiate_with(bytes([IAC, DO, 99]))

    async def test_negotiate_not_connected_early_return(self) -> None:
        """_negotiate() returns immediately if writer is None."""
        from provide.uterm.transports.telnet import DO

        t = TelnetTransport()
        # _writer is None — should return immediately without error
        await t._negotiate(DO, 1)

    async def test_handle_subnegotiation_no_writer_returns(self) -> None:
        """_handle_subnegotiation() returns immediately if writer is None."""
        t = TelnetTransport()
        await t._handle_subnegotiation(b"\x18\x01")  # OPT_TTYPE SEND

    async def test_send_cmd_writer_closing_returns(self) -> None:
        """_send_cmd() returns immediately if writer is None."""
        from provide.uterm.transports.telnet import DO

        t = TelnetTransport()
        await t._send_cmd(DO, 1)  # should not raise

    async def test_send_wont_deduplication(self) -> None:
        """_send_wont() only sends once for a given option."""
        server, port = await _make_server_that_sends(b"")
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t._send_wont(99)
            await t._send_wont(99)  # second call should be no-op
            assert 99 in t._negotiated["wont"]
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_send_do_deduplication(self) -> None:
        """_send_do() only sends once for a given option."""
        server, port = await _make_server_that_sends(b"")
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t._send_do(99)
            await t._send_do(99)  # second call should be no-op
            assert 99 in t._negotiated["do"]
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_send_dont_deduplication(self) -> None:
        """_send_dont() only sends once for a given option."""
        server, port = await _make_server_that_sends(b"")
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t._send_dont(99)
            await t._send_dont(99)  # second call should be no-op
            assert 99 in t._negotiated["dont"]
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_send_naws_not_connected_returns(self) -> None:
        """_send_naws() returns immediately if writer is None."""
        t = TelnetTransport()
        await t._send_naws(80, 25)  # should not raise

    async def test_send_subnegotiation_not_connected_returns(self) -> None:
        """_send_subnegotiation() returns immediately if writer is None."""
        t = TelnetTransport()
        await t._send_subnegotiation(b"\x18\x00ANSI")  # should not raise

    async def test_receive_connection_closed_raises(self) -> None:
        """Receive raises ConnectionError when remote closes connection."""

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()  # Close immediately after connect

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.sleep(0.05)
            with pytest.raises(ConnectionError):
                await t.receive(4096, timeout_ms=500)
        finally:
            server.close()
            await server.wait_closed()


class TestConsumeRxBufferZeroConsumed:
    def test_incomplete_iac_does_not_modify_buffer(self) -> None:
        """Line 248->250: consumed=0 when buffer starts with incomplete IAC → buffer unchanged."""
        t = TelnetTransport()
        # Single IAC byte — incomplete sequence, nothing can be consumed
        t._rx_buf = bytearray([IAC])
        payload, events = t._consume_rx_buffer()
        assert payload == b""
        assert events == []
        # Buffer must be unchanged since consumed=0
        assert t._rx_buf == bytearray([IAC])
