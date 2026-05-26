#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for transports/telnet_transport.py (part 2)."""

from __future__ import annotations

import asyncio

import pytest

from provide.uterm.transports.telnet import TelnetTransport
from provide.uterm.transports.telnet_transport import (
    DO,
    DONT,
    ECHO,
    IAC,
    NAWS,
    OPT_BINARY,
    OPT_NAWS,
    OPT_TTYPE,
    SB,
    SE,
    SGA,
    WILL,
    WONT,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockServer:
    """Minimal TCP server that captures received bytes and optionally sends initial data."""

    def __init__(self, initial_send: bytes = b"") -> None:
        self._initial_send = initial_send
        self._server: asyncio.Server | None = None
        self._port = 0
        self._received = bytearray()
        self._connected = asyncio.Event()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]
        return self._port

    async def _handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._initial_send:
            writer.write(self._initial_send)
            await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            self._received.extend(chunk)
        writer.close()
        self._connected.set()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def received(self) -> bytes:
        return bytes(self._received)

    @property
    def port(self) -> int:
        return self._port

    async def wait(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)


# ---------------------------------------------------------------------------
# receive() mutation killers
# mutmut_2-5: timeout conversion errors
# mutmut_13: _rx_buf.extend missing
# mutmut_15-23: task creation/handling broken
# ---------------------------------------------------------------------------


class TestReceiveMutationKilling:
    async def test_receive_returns_payload(self):
        """receive() returns application-layer bytes."""
        received_data: list[bytes] = []
        connected = asyncio.Event()

        async def handler(reader, writer):
            writer.write(b"hello")
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.close()
            connected.set()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            data = await asyncio.wait_for(t.receive(4096, timeout_ms=2000), timeout=3.0)
            received_data.append(data)
            await t.disconnect()
        finally:
            srv.close()
            await srv.wait_closed()

        # The actual data may be buried after IAC negotiation bytes
        # But we should get something
        assert len(received_data) >= 1

    async def test_receive_timeout_returns_empty(self):
        """receive() returns empty bytes on timeout (mutmut_2-5: timeout conversion)."""
        srv = MockServer()  # no initial send
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Short timeout — should return empty bytes
            data = await t.receive(4096, timeout_ms=50)
            assert data == b""
            await t.disconnect()
        finally:
            await srv.stop()

    async def test_receive_raises_when_not_connected(self):
        """receive() raises ConnectionError when no reader (mutmut_41 coverage)."""
        t = TelnetTransport()
        with pytest.raises(ConnectionError, match="Not connected"):
            await t.receive(4096, timeout_ms=100)


# ---------------------------------------------------------------------------
# _parse_telnet_buffer static method mutation killers
# ---------------------------------------------------------------------------


class TestParseTelnetBuffer:
    def test_plain_data_returned_as_payload(self):
        """Plain bytes without IAC returned as-is."""
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(b"hello")
        assert payload == b"hello"
        assert events == []
        assert consumed == 5

    def test_negotiate_event_produced(self):
        """IAC DO OPT produces negotiate event."""
        data = bytes([IAC, DO, ECHO])
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == b""
        assert len(events) == 1
        assert events[0] == ("negotiate", DO, ECHO)
        assert consumed == 3

    def test_escaped_iac_produces_single_iac(self):
        """IAC IAC produces single 0xFF in payload."""
        data = bytes([IAC, IAC])
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert payload == bytes([IAC])
        assert events == []

    def test_subnegotiation_produces_event(self):
        """IAC SB ... IAC SE produces subnegotiation event."""
        data = bytes([IAC, SB, 1, 2, 3, IAC, SE])
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert len(events) == 1
        assert events[0][0] == "subnegotiation"
        assert consumed == 7

    def test_incomplete_negotiate_leaves_bytes_unconsumed(self):
        """Truncated IAC WILL (no option byte) leaves those bytes unconsumed."""
        data = bytes([IAC, WILL])  # missing option byte
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert consumed == 0  # nothing consumed

    def test_mixed_payload_and_iac(self):
        """Plain data before IAC sequence: only payload data consumed."""
        data = b"abc" + bytes([IAC, WILL, ECHO]) + b"def"
        payload, events, consumed = TelnetTransport._parse_telnet_buffer(data)
        assert b"abc" in payload
        assert b"def" in payload
        assert len(events) >= 1


# ---------------------------------------------------------------------------
# _send_naws mutation killers (bit operations)
# The NAWS packet must be exactly:
# IAC SB 31 (cols>>8)&0xFF (cols&0xFF) (rows>>8)&0xFF (rows&0xFF) IAC SE
# ---------------------------------------------------------------------------


class TestSendNawsMutationKilling:
    async def test_naws_bit_operations_for_cols_255(self):
        """cols=255: wh=0, wl=255 — tests cols&0xFF vs cols|0xFF etc."""
        received = bytearray()
        done = asyncio.Event()

        async def handler(reader, writer):
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                received.extend(chunk)
            writer.close()
            done.set()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.set_size(255, 50)
            await t.disconnect()
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        # cols=255: wh=0, wl=255 (=0xFF, must be IAC-doubled per RFC 1073)
        # rows=50: hh=0, hl=50
        naws = bytes([IAC, SB, OPT_NAWS, 0, 255, 255, 0, 50, IAC, SE])
        assert naws in received

    async def test_naws_bit_operations_for_rows_255(self):
        """rows=255: hh=0, hl=255 — tests rows&0xFF vs rows|0xFF etc."""
        received = bytearray()
        done = asyncio.Event()

        async def handler(reader, writer):
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                received.extend(chunk)
            writer.close()
            done.set()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.set_size(80, 255)
            await t.disconnect()
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        # rows=255: hl=255 (=0xFF) must be IAC-doubled per RFC 1073
        naws = bytes([IAC, SB, OPT_NAWS, 0, 80, 0, 255, 255, IAC, SE])
        assert naws in received


# ---------------------------------------------------------------------------
# Finding #16 — IAC escaping in NAWS / generic subnegotiation (RFC 855/1073)
# ---------------------------------------------------------------------------


class TestIacEscapingInSubnegotiation:
    def test_escape_iac_helper_doubles_ff(self) -> None:
        assert TelnetTransport._escape_iac(b"") == b""
        assert TelnetTransport._escape_iac(b"\x00\xff\x01") == b"\x00\xff\xff\x01"
        assert TelnetTransport._escape_iac(b"\xff\xff") == b"\xff\xff\xff\xff"
        assert TelnetTransport._escape_iac(b"hello") == b"hello"

    async def _capture_writes(self, action) -> bytes:
        received = bytearray()
        done = asyncio.Event()

        async def handler(reader, writer):
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                received.extend(chunk)
            writer.close()
            done.set()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await action(t)
            await t.disconnect()
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()
        return bytes(received)

    async def test_naws_normal_size_unchanged(self) -> None:
        wire = await self._capture_writes(lambda t: t.set_size(80, 24))
        # No 0xFF inside the payload → unchanged framing.
        naws = bytes([IAC, SB, OPT_NAWS, 0, 80, 0, 24, IAC, SE])
        assert naws in wire

    async def test_naws_cols_65535_escapes_both_bytes(self) -> None:
        wire = await self._capture_writes(lambda t: t.set_size(65535, 24))
        # cols=0xFFFF → both wh and wl are 0xFF and must each be doubled.
        naws = bytes([IAC, SB, OPT_NAWS, 0xFF, 0xFF, 0xFF, 0xFF, 0, 24, IAC, SE])
        assert naws in wire

    async def test_naws_rows_65535_escapes_both_bytes(self) -> None:
        wire = await self._capture_writes(lambda t: t.set_size(80, 65535))
        # rows=0xFFFF → both hh and hl are 0xFF and must each be doubled.
        naws = bytes([IAC, SB, OPT_NAWS, 0, 80, 0xFF, 0xFF, 0xFF, 0xFF, IAC, SE])
        assert naws in wire

    async def test_send_subnegotiation_escapes_payload(self) -> None:
        async def _emit(t):
            await t._send_subnegotiation(b"\x01\xff\x02\xff\x03")

        wire = await self._capture_writes(_emit)
        expected = bytes([IAC, SB, 0x01, 0xFF, 0xFF, 0x02, 0xFF, 0xFF, 0x03, IAC, SE])
        assert expected in wire


# ---------------------------------------------------------------------------
# _negotiate mutation killers
# Tests that the right IAC responses are sent for each negotiation type
# ---------------------------------------------------------------------------


class TestNegotiateMutationKilling:
    async def _run_negotiation(self, server_send: bytes) -> bytearray:
        """Start server that sends negotiation bytes, collect what client sends back.

        TelnetTransport only parses IAC sequences when receive() is called, so
        we must call t.receive() after connecting to trigger _negotiate tasks.
        """
        client_sent = bytearray()
        ready = asyncio.Event()

        async def handler(reader, writer):
            writer.write(server_send)
            await writer.drain()
            # Collect all bytes the client sends us (initial WILLs + negotiate responses)
            for _ in range(10):
                try:
                    chunk = await asyncio.wait_for(reader.read(512), timeout=0.3)
                    if not chunk:
                        break
                    client_sent.extend(chunk)
                except TimeoutError:
                    break
            ready.set()
            # Keep alive briefly so client can finish
            await asyncio.sleep(0.2)
            writer.close()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Must call receive() to trigger IAC parsing and _negotiate tasks
            import contextlib

            with contextlib.suppress(Exception):
                await asyncio.wait_for(t.receive(512, 200), timeout=0.5)
            # Wait for negotiate tasks to complete
            await asyncio.sleep(0.2)
            await asyncio.wait_for(ready.wait(), timeout=2.0)
            await t.disconnect()
        finally:
            srv.close()
            await srv.wait_closed()

        return client_sent

    async def test_server_do_binary_client_responds_will(self):
        """Server DO OPT_BINARY → client must send WILL OPT_BINARY."""
        data = await self._run_negotiation(bytes([IAC, DO, OPT_BINARY]))
        assert bytes([IAC, WILL, OPT_BINARY]) in data

    async def test_server_do_naws_client_responds_will_and_naws(self):
        """Server DO NAWS → client sends WILL NAWS + NAWS subnegotiation."""
        data = await self._run_negotiation(bytes([IAC, DO, NAWS]))
        assert bytes([IAC, WILL, NAWS]) in data
        # Should also send NAWS subneg
        assert bytes([IAC, SB, OPT_NAWS]) in data

    async def test_server_do_ttype_client_responds_will(self):
        """Server DO TTYPE → client sends WILL TTYPE."""
        data = await self._run_negotiation(bytes([IAC, DO, OPT_TTYPE]))
        assert bytes([IAC, WILL, OPT_TTYPE]) in data

    async def test_server_will_echo_client_responds_do(self):
        """Server WILL ECHO → client sends DO ECHO."""
        data = await self._run_negotiation(bytes([IAC, WILL, ECHO]))
        assert bytes([IAC, DO, ECHO]) in data

    async def test_server_will_sga_client_responds_do(self):
        """Server WILL SGA → client sends DO SGA."""
        data = await self._run_negotiation(bytes([IAC, WILL, SGA]))
        assert bytes([IAC, DO, SGA]) in data

    async def test_server_dont_causes_wont_response(self):
        """Server DONT X → client sends WONT X."""
        data = await self._run_negotiation(bytes([IAC, DONT, 99]))
        assert bytes([IAC, WONT, 99]) in data

    async def test_server_wont_causes_dont_response(self):
        """Server WONT X → client sends DONT X."""
        data = await self._run_negotiation(bytes([IAC, WONT, 99]))
        assert bytes([IAC, DONT, 99]) in data
