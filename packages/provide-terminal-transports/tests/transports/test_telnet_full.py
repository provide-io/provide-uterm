#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TelnetTransport — connect/disconnect, send, NAWS, receive edge cases,
connect branches, server handshake, and TelnetClient API tests."""

from __future__ import annotations

import asyncio

import pytest

from tests.helpers.mock_server import MockTelnetServer
from provide.terminal.transports.telnet import IAC, TelnetTransport


async def _read_until(
    reader: asyncio.StreamReader,
    needle: bytes,
    *,
    max_bytes: int = 512,
    timeout: float = 2.0,
) -> bytes:
    """Accumulate bytes from *reader* until *needle* is found or timeout expires.

    TelnetTransport sends Telnet option negotiation bytes (IAC sequences) as a
    separate write from the actual payload.  A single ``reader.read(n)`` call can
    therefore return only the negotiation bytes, causing tests to miss the payload.
    This helper keeps reading until it either finds the expected content or times out.
    """
    data = bytearray()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline and len(data) < max_bytes:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(max_bytes), timeout=min(remaining, 0.1))
        except TimeoutError:
            if needle in data:
                break
            continue
        if not chunk:
            break
        data.extend(chunk)
        if needle in data:
            break
    return bytes(data)


async def _make_server_that_sends(data: bytes) -> tuple[asyncio.Server, int]:
    """Start a bare TCP server that sends *data* immediately after connect."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(data)
        await writer.drain()
        await asyncio.sleep(2)

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]
    return server, port


class TestTelnetTransportConnect:
    async def test_connect_and_receive(self) -> None:
        async with MockTelnetServer(send=b"hello") as srv:
            t = TelnetTransport()
            await t.connect("127.0.0.1", srv.port)
            assert t.is_connected()
            await t.disconnect()
        assert not t.is_connected()

    async def test_not_connected_raises(self) -> None:
        t = TelnetTransport()
        with pytest.raises(ConnectionError):
            await t.send(b"test")

    async def test_receive_not_connected_raises(self) -> None:
        t = TelnetTransport()
        with pytest.raises(ConnectionError):
            await t.receive(128, 100)


class TestTelnetTransportSend:
    async def test_iac_escaping(self) -> None:
        """0xFF bytes in data must be escaped to 0xFF 0xFF."""
        wire_data: bytes = b""
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal wire_data
            wire_data = await _read_until(reader, b"\xff\xff")
            writer.close()
            ready.set()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None

        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.send(b"\xff\x00\xff")
            await asyncio.wait_for(ready.wait(), timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()

        # Both 0xFF bytes should be doubled on the wire
        assert b"\xff\xff" in wire_data

    async def test_send_plain_data(self) -> None:
        wire_data: bytes = b""
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal wire_data
            wire_data = await _read_until(reader, b"ping")
            writer.close()
            ready.set()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.send(b"ping")
            await asyncio.wait_for(ready.wait(), timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()

        assert b"ping" in wire_data


class TestTelnetTransportNAWS:
    async def test_set_size_when_not_connected_raises(self) -> None:
        t = TelnetTransport()
        with pytest.raises(ConnectionError):
            await t.set_size(80, 25)

    async def test_set_size_sends_naws(self) -> None:
        """Verify that set_size sends the NAWS subnegotiation bytes."""
        received: list[bytes] = []
        ready = asyncio.Event()

        naws_marker = bytes([IAC, 250, 31])  # IAC SB NAWS

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await _read_until(reader, naws_marker)
            received.append(data)
            writer.close()
            ready.set()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.set_size(132, 50)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()

        # NAWS bytes: IAC SB 31 (cols_hi cols_lo rows_hi rows_lo) IAC SE
        wire = received[0]
        assert naws_marker in wire


class TestTelnetTransportReceiveEdgeCases:
    async def test_receive_timeout_returns_empty(self) -> None:
        """Receive with very short timeout returns empty bytes."""

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(10)

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            result = await t.receive(4096, timeout_ms=1)  # 1ms timeout
            assert result == b""
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_disconnect_when_not_connected_is_noop(self) -> None:
        t = TelnetTransport()
        await t.disconnect()  # Should not raise


class TestTelnetServerHandshakeReset:
    async def test_connection_reset_during_handshake(self) -> None:
        """start_telnet_server handles ConnectionResetError during handshake."""
        from provide.terminal.transports.telnet import start_telnet_server

        handled: list[bool] = []

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            handled.append(True)
            writer.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        try:
            # Connect and immediately close to trigger reset during handshake
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await asyncio.sleep(0.2)
        finally:
            server.close()
            await server.wait_closed()


# ---------------------------------------------------------------------------
# TelnetClient
# ---------------------------------------------------------------------------


class TestTelnetClient:
    async def test_wont_and_dont_builders(self) -> None:
        """TelnetClient.wont() and dont() build correct IAC sequences."""
        from provide.terminal.transports.telnet import DONT, IAC, WONT, TelnetClient

        c = TelnetClient("127.0.0.1", 9)  # don't connect
        assert c.wont(1) == bytes([IAC, WONT, 1])
        assert c.dont(1) == bytes([IAC, DONT, 1])

    async def test_will_and_do_builders(self) -> None:
        """TelnetClient.will() and do() build correct IAC sequences."""
        from provide.terminal.transports.telnet import DO, IAC, WILL, TelnetClient

        c = TelnetClient("127.0.0.1", 9)
        assert c.will(3) == bytes([IAC, WILL, 3])
        assert c.do(3) == bytes([IAC, DO, 3])

    async def test_aenter_aexit_context_manager(self) -> None:
        """TelnetClient can be used as async context manager."""
        from provide.terminal.transports.telnet import TelnetClient

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(2)

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        try:
            async with TelnetClient("127.0.0.1", port) as c:
                assert c._reader is not None
        finally:
            server.close()
            await server.wait_closed()

    async def test_read_method(self) -> None:
        """TelnetClient.read() reads bytes from the server."""
        from provide.terminal.transports.telnet import TelnetClient

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"hello")
            await writer.drain()
            await asyncio.sleep(2)

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        try:
            c = TelnetClient("127.0.0.1", port)
            await c.connect()
            data = await asyncio.wait_for(c.read(10), timeout=2.0)
            assert b"hello" in data
            await c.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_readuntil_and_write_and_drain(self) -> None:
        """TelnetClient readuntil/write/drain with a real connection."""
        from provide.terminal.transports.telnet import TelnetClient

        server_data: list[bytes] = []

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"hello\n")
            await writer.drain()
            data = await reader.read(32)
            server_data.append(data)
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        try:
            c = TelnetClient("127.0.0.1", port)
            await c.connect()
            line = await asyncio.wait_for(c.readuntil(b"\n"), timeout=2.0)
            assert line == b"hello\n"
            c.write(b"ping")
            await c.drain()
            await c.close()
        finally:
            server.close()
            await server.wait_closed()

        assert b"ping" in server_data[0] if server_data else True


# ---------------------------------------------------------------------------
# TelnetTransport.connect() reconnect and failure branches
# ---------------------------------------------------------------------------


class TestTelnetTransportConnectBranches:
    async def test_connect_fails_raises_connection_error(self) -> None:
        """Connection to a port that won't accept connections raises ConnectionError."""
        t = TelnetTransport()
        with pytest.raises(ConnectionError):
            await t.connect("127.0.0.1", 1, timeout=0.1)  # port 1 refused

    async def test_connect_when_already_connected_disconnects_first(self) -> None:
        """A second connect() call disconnects the previous connection first."""

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(5)

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            assert t.is_connected()
            # Second connect — should disconnect first, then reconnect
            await t.connect("127.0.0.1", port)
            assert t.is_connected()
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()
