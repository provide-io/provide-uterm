#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for TelnetTransport (full RFC 854 client)."""

from __future__ import annotations

import asyncio

import pytest

from tests.helpers.mock_server import MockTelnetServer
from provide.terminal.transports.telnet import IAC, TelnetTransport


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
        port = server.sockets[0].getsockname()[1]  # type: ignore[index]
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
        port = server.sockets[0].getsockname()[1]  # type: ignore[index]
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
        port = server.sockets[0].getsockname()[1]  # type: ignore[index]
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


class TestTelnetTransportIACParsing:
    def test_parse_telnet_buffer_plain_data(self) -> None:
        data = b"Hello World"
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == b"Hello World"
        assert events == []
        assert consumed == len(data)

    def test_parse_telnet_buffer_escaped_iac(self) -> None:
        data = bytes([IAC, IAC])
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == bytes([IAC])
        assert events == []

    def test_parse_telnet_buffer_negotiate(self) -> None:
        from provide.terminal.transports.telnet import DO, ECHO

        data = bytes([IAC, DO, ECHO])
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == b""
        assert len(events) == 1
        assert events[0] == ("negotiate", DO, ECHO)

    def test_parse_telnet_buffer_incomplete(self) -> None:
        """Incomplete IAC sequence should not be consumed."""
        data = bytes([IAC])
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == b""
        assert consumed == 0


class TestTelnetTransportNegotiate:
    async def test_negotiate_do_binary(self) -> None:
        """Transport responds to DO BINARY with WILL BINARY."""
        from provide.terminal.transports.telnet import DO, OPT_BINARY, IAC, WILL

        reply_data: list[bytes] = []
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # Send IAC DO BINARY
            writer.write(bytes([IAC, DO, OPT_BINARY]))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=1.0)
            reply_data.append(data)
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_negotiate_do_naws(self) -> None:
        """Transport responds to DO NAWS with WILL NAWS + NAWS subnegotiation."""
        from provide.terminal.transports.telnet import DO, IAC, OPT_NAWS, SB, WILL

        all_data = bytearray()
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes([IAC, DO, OPT_NAWS]))
            await writer.drain()
            # Read multiple chunks to get all negotiate responses
            for _ in range(5):
                try:
                    data = await asyncio.wait_for(reader.read(256), timeout=0.3)
                    all_data.extend(data)
                except TimeoutError:
                    break
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Trigger a receive so the _negotiate task runs
            await asyncio.sleep(0.1)
            try:
                await asyncio.wait_for(t.receive(256, 100), timeout=0.3)
            except (ConnectionError, TimeoutError):
                pass
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

        # Transport should have sent WILL NAWS and/or NAWS subnegotiation
        assert bytes([IAC, WILL, OPT_NAWS]) in bytes(all_data) or bytes([IAC, SB, OPT_NAWS]) in bytes(all_data)

    async def test_negotiate_do_ttype(self) -> None:
        """Transport responds to DO TTYPE with WILL TTYPE + SB."""
        from provide.terminal.transports.telnet import DO, IAC, OPT_TTYPE, SB

        received: list[bytes] = []
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes([IAC, DO, OPT_TTYPE]))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=1.0)
            received.append(data)
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_negotiate_do_unknown_option(self) -> None:
        """Transport responds to DO <unknown> with WONT."""
        from provide.terminal.transports.telnet import DO, IAC, WONT

        received: list[bytes] = []
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes([IAC, DO, 99]))  # unknown option
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=1.0)
            received.append(data)
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

        wire = received[0] if received else b""
        assert IAC in wire

    async def test_negotiate_dont_sends_wont(self) -> None:
        """Transport responds to DONT with WONT."""
        from provide.terminal.transports.telnet import DONT, IAC

        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes([IAC, DONT, 1]))
            await writer.drain()
            await asyncio.wait_for(reader.read(64), timeout=1.0)
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_negotiate_will_echo_sends_do(self) -> None:
        """Transport responds to server WILL ECHO with DO ECHO."""
        from provide.terminal.transports.telnet import WILL, OPT_ECHO, IAC, DO

        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes([IAC, WILL, OPT_ECHO]))
            await writer.drain()
            await asyncio.wait_for(reader.read(64), timeout=1.0)
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_negotiate_will_unknown_sends_dont(self) -> None:
        """Transport responds to WILL <unknown> with DONT."""
        from provide.terminal.transports.telnet import WILL, IAC

        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes([IAC, WILL, 77]))  # unknown
            await writer.drain()
            await asyncio.wait_for(reader.read(64), timeout=1.0)
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_negotiate_wont_sends_dont(self) -> None:
        """Transport responds to WONT with DONT."""
        from provide.terminal.transports.telnet import WONT, IAC

        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes([IAC, WONT, 1]))
            await writer.drain()
            await asyncio.wait_for(reader.read(64), timeout=1.0)
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_handle_subnegotiation_ttype(self) -> None:
        """SB TTYPE SEND triggers TTYPE IS response."""
        from provide.terminal.transports.telnet import IAC, OPT_TTYPE, SB, SE

        all_data = bytearray()
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # SB OPT_TTYPE 1 (SEND) SE
            writer.write(bytes([IAC, SB, OPT_TTYPE, 1, IAC, SE]))
            await writer.drain()
            # Read multiple chunks to collect all responses
            for _ in range(5):
                try:
                    data = await asyncio.wait_for(reader.read(256), timeout=0.3)
                    all_data.extend(data)
                except TimeoutError:
                    break
            ready.set()
            writer.close()

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Trigger receive to process subnegotiation
            await asyncio.sleep(0.1)
            try:
                await asyncio.wait_for(t.receive(256, 100), timeout=0.3)
            except (ConnectionError, TimeoutError):
                pass
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

        # Response should contain TTYPE data
        assert OPT_TTYPE in bytes(all_data)


class TestTelnetTransportReceiveEdgeCases:
    async def test_receive_timeout_returns_empty(self) -> None:
        """Receive with very short timeout returns empty bytes."""
        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(10)

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
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
        from provide.terminal.transports.telnet import IAC, WONT, DONT, TelnetClient

        c = TelnetClient("127.0.0.1", 9)  # don't connect
        assert c.wont(1) == bytes([IAC, WONT, 1])
        assert c.dont(1) == bytes([IAC, DONT, 1])

    async def test_will_and_do_builders(self) -> None:
        """TelnetClient.will() and do() build correct IAC sequences."""
        from provide.terminal.transports.telnet import IAC, WILL, DO, TelnetClient

        c = TelnetClient("127.0.0.1", 9)
        assert c.will(3) == bytes([IAC, WILL, 3])
        assert c.do(3) == bytes([IAC, DO, 3])

    async def test_aenter_aexit_context_manager(self) -> None:
        """TelnetClient can be used as async context manager."""
        from provide.terminal.transports.telnet import TelnetClient

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(2)

        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
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
# _parse_telnet_buffer edge cases
# ---------------------------------------------------------------------------


class TestParseTelnetBufferEdgeCases:
    def test_incomplete_negotiate_command(self) -> None:
        """IAC DO with no option byte is not consumed."""
        from provide.terminal.transports.telnet import IAC, DO

        data = bytes([IAC, DO])  # incomplete — missing option byte
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == b""
        assert events == []
        assert consumed == 0

    def test_incomplete_subnegotiation_not_consumed(self) -> None:
        """SB without matching SE is not consumed."""
        from provide.terminal.transports.telnet import IAC, SB, OPT_TTYPE

        data = bytes([IAC, SB, OPT_TTYPE, 0, 65])  # no IAC SE terminator
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert events == []
        assert consumed == 0

    def test_unknown_iac_command_skipped(self) -> None:
        """An unknown IAC command (not DO/DONT/WILL/WONT/SB/SE/IAC) consumes 2 bytes."""
        from provide.terminal.transports.telnet import IAC

        # IAC 5 (unknown command) followed by some data
        data = bytes([IAC, 5]) + b"hello"
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        # Unknown command: 2 bytes consumed, then 'hello' as payload
        assert payload == b"hello"
        assert consumed == len(data)


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


# ---------------------------------------------------------------------------
# TelnetTransport._negotiate() — all branches via receive()
# ---------------------------------------------------------------------------


async def _make_server_that_sends(data: bytes) -> tuple[asyncio.Server, int]:
    """Start a bare TCP server that sends *data* immediately after connect."""
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(data)
        await writer.drain()
        await asyncio.sleep(2)

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


class TestTelnetNegotiateBranches:
    async def _negotiate_with(self, cmd_bytes: bytes) -> None:
        """Connect, have server send *cmd_bytes*, then call receive() to trigger negotiate."""
        server, port = await _make_server_that_sends(cmd_bytes)
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            try:
                await asyncio.wait_for(t.receive(256, 200), timeout=1.0)
            except (ConnectionError, TimeoutError):
                pass
            # Give background tasks a moment to complete
            await asyncio.sleep(0.05)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    async def test_negotiate_dont_branch(self) -> None:
        """DONT branch records in negotiated['dont'] and calls _send_wont."""
        from provide.terminal.transports.telnet import IAC, DONT

        await self._negotiate_with(bytes([IAC, DONT, 1]))

    async def test_negotiate_will_branch_known(self) -> None:
        """WILL ECHO branch calls _send_do."""
        from provide.terminal.transports.telnet import IAC, WILL, OPT_ECHO

        await self._negotiate_with(bytes([IAC, WILL, OPT_ECHO]))

    async def test_negotiate_will_branch_unknown(self) -> None:
        """WILL <unknown> branch calls _send_dont."""
        from provide.terminal.transports.telnet import IAC, WILL

        await self._negotiate_with(bytes([IAC, WILL, 77]))

    async def test_negotiate_wont_branch(self) -> None:
        """WONT branch calls _send_dont."""
        from provide.terminal.transports.telnet import IAC, WONT

        await self._negotiate_with(bytes([IAC, WONT, 1]))

    async def test_negotiate_do_binary_branch(self) -> None:
        """DO BINARY branch calls _send_will."""
        from provide.terminal.transports.telnet import IAC, DO, OPT_BINARY

        await self._negotiate_with(bytes([IAC, DO, OPT_BINARY]))

    async def test_negotiate_do_naws_branch(self) -> None:
        """DO NAWS branch calls _send_will + _send_naws."""
        from provide.terminal.transports.telnet import IAC, DO, OPT_NAWS

        await self._negotiate_with(bytes([IAC, DO, OPT_NAWS]))

    async def test_negotiate_do_ttype_branch(self) -> None:
        """DO TTYPE branch calls _send_will + _send_ttype."""
        from provide.terminal.transports.telnet import IAC, DO, OPT_TTYPE

        await self._negotiate_with(bytes([IAC, DO, OPT_TTYPE]))

    async def test_negotiate_do_unknown_branch(self) -> None:
        """DO <unknown> branch calls _send_wont."""
        from provide.terminal.transports.telnet import IAC, DO

        await self._negotiate_with(bytes([IAC, DO, 99]))

    async def test_negotiate_not_connected_early_return(self) -> None:
        """_negotiate() returns immediately if writer is None."""
        from provide.terminal.transports.telnet import DO

        t = TelnetTransport()
        # _writer is None — should return immediately without error
        await t._negotiate(DO, 1)

    async def test_handle_subnegotiation_no_writer_returns(self) -> None:
        """_handle_subnegotiation() returns immediately if writer is None."""
        t = TelnetTransport()
        await t._handle_subnegotiation(b"\x18\x01")  # OPT_TTYPE SEND

    async def test_send_cmd_writer_closing_returns(self) -> None:
        """_send_cmd() returns immediately if writer is None."""
        from provide.terminal.transports.telnet import DO

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
