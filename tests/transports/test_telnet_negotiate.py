#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for TelnetTransport IAC parsing and negotiation — static parse tests,
IAC parsing edge cases, and telnet negotiation integration tests."""

from __future__ import annotations

import asyncio
import contextlib

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
    port = server.sockets[0].getsockname()[1]
    return server, port


# ---------------------------------------------------------------------------
# IAC parsing static tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# IAC parsing edge cases
# ---------------------------------------------------------------------------


class TestParseTelnetBufferEdgeCases:
    def test_incomplete_negotiate_command(self) -> None:
        """IAC DO with no option byte is not consumed."""
        from provide.terminal.transports.telnet import DO, IAC

        data = bytes([IAC, DO])  # incomplete — missing option byte
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == b""
        assert events == []
        assert consumed == 0

    def test_incomplete_subnegotiation_not_consumed(self) -> None:
        """SB without matching SE is not consumed."""
        from provide.terminal.transports.telnet import IAC, OPT_TTYPE, SB

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
# Telnet negotiation integration tests
# ---------------------------------------------------------------------------


class TestTelnetTransportNegotiate:
    async def test_negotiate_do_binary(self) -> None:
        """Transport responds to DO BINARY with WILL BINARY."""
        from provide.terminal.transports.telnet import DO, IAC, OPT_BINARY

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
            with contextlib.suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(t.receive(256, 100), timeout=0.3)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

        # Transport should have sent WILL NAWS and/or NAWS subnegotiation
        assert bytes([IAC, WILL, OPT_NAWS]) in bytes(all_data) or bytes([IAC, SB, OPT_NAWS]) in bytes(all_data)

    async def test_negotiate_do_ttype(self) -> None:
        """Transport responds to DO TTYPE with WILL TTYPE + SB."""
        from provide.terminal.transports.telnet import DO, IAC, OPT_TTYPE

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
        from provide.terminal.transports.telnet import DO, IAC

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
        from provide.terminal.transports.telnet import IAC, OPT_ECHO, WILL

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
        from provide.terminal.transports.telnet import IAC, WILL

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
        from provide.terminal.transports.telnet import IAC, WONT

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
            with contextlib.suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(t.receive(256, 100), timeout=0.3)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            server.close()
            await server.wait_closed()

        # Response should contain TTYPE data
        assert OPT_TTYPE in bytes(all_data)

    async def test_handle_subnegotiation_non_ttype_does_not_send_ttype(self) -> None:
        """SB with a non-TTYPE option should NOT trigger _send_ttype."""
        from unittest.mock import AsyncMock, patch

        from provide.terminal.transports.telnet import IAC, SB, SE

        # Use option 99 (not OPT_TTYPE) — _send_ttype must not be called
        non_ttype_option = 99
        all_data = bytearray()
        ready = asyncio.Event()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # Send SB with a non-TTYPE option: IAC SB <opt> 1 IAC SE
            writer.write(bytes([IAC, SB, non_ttype_option, 1, IAC, SE]))
            await writer.drain()
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
            with patch.object(t, "_send_ttype", new_callable=AsyncMock) as mock_send_ttype:
                await t.connect("127.0.0.1", port)
                await asyncio.sleep(0.1)
                with contextlib.suppress(ConnectionError, TimeoutError):
                    await asyncio.wait_for(t.receive(256, 100), timeout=0.3)
                await asyncio.wait_for(ready.wait(), timeout=2.0)
                await t.disconnect()

                mock_send_ttype.assert_not_called()
        finally:
            server.close()
            await server.wait_closed()
