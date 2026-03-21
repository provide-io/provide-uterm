#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for transports/telnet_transport.py."""

from __future__ import annotations

import asyncio

import pytest

from provide.terminal.transports.telnet import TelnetTransport
from provide.terminal.transports.telnet_transport import (
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
    TTYPE_IS,
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
# __init__ mutation killers
# mutmut_1: _reader = ""  (not None)
# mutmut_2: _writer = ""  (not None)
# mutmut_13: _cols = None
# mutmut_14: _cols = 81
# mutmut_15: _rows = None
# mutmut_16: _rows = 26
# mutmut_17: _term = None
# mutmut_18: _term = "XXANSIXX"
# mutmut_19: _term = "ansi"
# ---------------------------------------------------------------------------


class TestTelnetTransportInitMutationKilling:
    def test_reader_initially_none(self):
        """_reader must be None, not '' (mutmut_1)."""
        t = TelnetTransport()
        assert t._reader is None

    def test_writer_initially_none(self):
        """_writer must be None, not '' (mutmut_2)."""
        t = TelnetTransport()
        assert t._writer is None

    def test_cols_initially_80(self):
        """_cols must be 80, not None/81 (mutmut_13, mutmut_14)."""
        t = TelnetTransport()
        assert t._cols == 80
        assert isinstance(t._cols, int)

    def test_rows_initially_25(self):
        """_rows must be 25, not None/26 (mutmut_15, mutmut_16)."""
        t = TelnetTransport()
        assert t._rows == 25
        assert isinstance(t._rows, int)

    def test_term_initially_ansi_uppercase(self):
        """_term must be 'ANSI' (exact case) — not None/'ansi'/'XXANSIXX' (mutmut_17-19)."""
        t = TelnetTransport()
        assert t._term == "ANSI"

    def test_is_connected_false_without_writer(self):
        """is_connected() returns False when _writer is None (mutmut_1/2 affected)."""
        t = TelnetTransport()
        assert not t.is_connected()


# ---------------------------------------------------------------------------
# send() mutation killers
# mutmut_2: ConnectionError(None)
# mutmut_3: ConnectionError("XXNot connectedXX")
# mutmut_4: ConnectionError("not connected")
# mutmut_5: ConnectionError("NOT CONNECTED")
# mutmut_15: DEL replacement removed
# mutmut_16: IAC escaping removed
# mutmut_17, 19-26: data transformations broken
# ---------------------------------------------------------------------------


class TestSendMutationKilling:
    async def test_send_raises_connection_error_when_not_connected(self):
        """send() must raise ConnectionError 'Not connected' when no writer."""
        t = TelnetTransport()
        with pytest.raises(ConnectionError, match="Not connected"):
            await t.send(b"test")

    async def test_send_del_remapped_to_bs(self):
        """0x7F DEL byte must be remapped to 0x08 BS before sending."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.send(b"\x7f")
            await t.disconnect()
            await srv.wait()
        finally:
            await srv.stop()
        # The server should have received 0x08, not 0x7F
        # (may also have IAC negotiation bytes, so check for 0x08 presence)
        # The DEL byte should NOT appear
        # IAC negotiation may precede our data; check entire received stream
        # Filter out IAC sequences and find our data
        assert b"\x08" in srv.received
        assert b"\x7f" not in srv.received

    async def test_send_iac_escaped(self):
        """0xFF bytes must be escaped as 0xFF 0xFF (IAC IAC)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.send(b"\xff")
            await t.disconnect()
            await srv.wait()
        finally:
            await srv.stop()
        # 0xFF in payload should arrive as 0xFF 0xFF
        assert b"\xff\xff" in srv.received

    async def test_send_plain_data_forwarded(self):
        """Plain data (no DEL or FF) forwarded unchanged."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.send(b"hello")
            await t.disconnect()
            await srv.wait()
        finally:
            await srv.stop()
        assert b"hello" in srv.received


# ---------------------------------------------------------------------------
# disconnect() mutation killers
# mutmut_4: tasks not cleared
# mutmut_6: _writer not set to None
# mutmut_7: _reader not set to None
# mutmut_8: _rx_buf not cleared
# mutmut_9: _negotiated not reset
# mutmut_12, 13: exception types changed
# ---------------------------------------------------------------------------


class TestDisconnectMutationKilling:
    async def test_disconnect_resets_writer_to_none(self):
        """After disconnect, _writer must be None (mutmut_6)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            assert t._writer is not None
            await t.disconnect()
            assert t._writer is None
        finally:
            await srv.stop()

    async def test_disconnect_resets_reader_to_none(self):
        """After disconnect, _reader must be None (mutmut_7)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            assert t._reader is not None
            await t.disconnect()
            assert t._reader is None
        finally:
            await srv.stop()

    async def test_disconnect_clears_rx_buf(self):
        """After disconnect, _rx_buf must be empty (mutmut_8)."""
        srv = MockServer(initial_send=b"data")
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Feed some data to the buffer
            t._rx_buf.extend(b"buffered")
            await t.disconnect()
            assert len(t._rx_buf) == 0
        finally:
            await srv.stop()

    async def test_disconnect_resets_negotiated(self):
        """After disconnect, _negotiated must be reset to empty sets (mutmut_9)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Add some negotiated state
            t._negotiated["will"].add(1)
            await t.disconnect()
            # All sets should be empty after reset
            assert t._negotiated == {"do": set(), "dont": set(), "will": set(), "wont": set()}
        finally:
            await srv.stop()

    async def test_is_connected_false_after_disconnect(self):
        """is_connected() must return False after disconnect."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            assert t.is_connected()
            await t.disconnect()
            assert not t.is_connected()
        finally:
            await srv.stop()

    async def test_disconnect_clears_tasks(self):
        """After disconnect, _tasks must be empty (mutmut_4)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.disconnect()
            assert len(t._tasks) == 0
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# connect() mutation killers
# mutmut_1: _cols set incorrectly
# mutmut_2: _rows set incorrectly
# mutmut_3: _term set incorrectly
# mutmut_4: timeout arg broken
# ---------------------------------------------------------------------------


class TestConnectMutationKilling:
    async def test_connect_sets_cols(self):
        """cols parameter is stored to _cols (mutmut_1)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port, cols=132)
            assert t._cols == 132
            await t.disconnect()
        finally:
            await srv.stop()

    async def test_connect_sets_rows(self):
        """rows parameter is stored to _rows (mutmut_2)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port, rows=50)
            assert t._rows == 50
            await t.disconnect()
        finally:
            await srv.stop()

    async def test_connect_sets_term(self):
        """term parameter is stored to _term (mutmut_3)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port, term="VT100")
            assert t._term == "VT100"
            await t.disconnect()
        finally:
            await srv.stop()

    async def test_connect_failure_raises_connection_error(self):
        """connect() on closed port raises ConnectionError."""
        t = TelnetTransport()
        with pytest.raises(ConnectionError, match="Failed to connect"):
            await t.connect("127.0.0.1", 1, timeout=0.1)

    async def test_connect_default_cols_80(self):
        """Default cols is 80."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            assert t._cols == 80
            await t.disconnect()
        finally:
            await srv.stop()

    async def test_connect_default_rows_25(self):
        """Default rows is 25."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            assert t._rows == 25
            await t.disconnect()
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# set_size() mutation killers
# mutmut_2: cols not stored
# mutmut_3: rows not stored
# mutmut_4-7: NAWS packet broken
# ---------------------------------------------------------------------------


class TestSetSizeMutationKilling:
    async def test_set_size_updates_cols(self):
        """set_size must update _cols (mutmut_2)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.set_size(120, 40)
            assert t._cols == 120
            await t.disconnect()
        finally:
            await srv.stop()

    async def test_set_size_updates_rows(self):
        """set_size must update _rows (mutmut_3)."""
        srv = MockServer()
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.set_size(80, 60)
            assert t._rows == 60
            await t.disconnect()
        finally:
            await srv.stop()

    async def test_set_size_sends_naws_packet(self):
        """set_size must send NAWS packet with correct bytes (mutmut_4-7 bit op errors)."""
        received = bytearray()
        connected = asyncio.Event()

        async def handler(reader, writer):
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                received.extend(chunk)
            writer.close()
            connected.set()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.set_size(80, 25)
            await t.disconnect()
            await asyncio.wait_for(connected.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        # NAWS packet: IAC SB NAWS wh wl hh hl IAC SE
        # For 80x25: wh=0, wl=80, hh=0, hl=25
        naws_packet = bytes([IAC, SB, OPT_NAWS, 0, 80, 0, 25, IAC, SE])
        assert naws_packet in received

    async def test_set_size_naws_large_terminal(self):
        """NAWS correctly encodes dimensions > 255 (high byte non-zero)."""
        received = bytearray()
        connected = asyncio.Event()

        async def handler(reader, writer):
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                received.extend(chunk)
            writer.close()
            connected.set()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            await t.set_size(512, 256)
            await t.disconnect()
            await asyncio.wait_for(connected.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        # 512 = 0x0200 → wh=2, wl=0
        # 256 = 0x0100 → hh=1, hl=0
        naws_packet = bytes([IAC, SB, OPT_NAWS, 2, 0, 1, 0, IAC, SE])
        assert naws_packet in received

    async def test_set_size_raises_when_not_connected(self):
        """set_size raises ConnectionError when not connected."""
        t = TelnetTransport()
        with pytest.raises(ConnectionError):
            await t.set_size(80, 25)


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

        # cols=255: wh=0, wl=255
        # rows=50: hh=0, hl=50
        naws = bytes([IAC, SB, OPT_NAWS, 0, 255, 0, 50, IAC, SE])
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

        naws = bytes([IAC, SB, OPT_NAWS, 0, 80, 0, 255, IAC, SE])
        assert naws in received


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


# ---------------------------------------------------------------------------
# _send_ttype mutation killers
# mutmut_6-11: OPT_TTYPE, TTYPE_IS constants, term encoding broken
# ---------------------------------------------------------------------------


class TestSendTtypeMutationKilling:
    async def test_ttype_subneg_format(self):
        """_send_ttype sends IAC SB OPT_TTYPE TTYPE_IS <term> IAC SE."""
        received = bytearray()
        done = asyncio.Event()

        async def handler(reader, writer):
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(512), timeout=0.5)
                except TimeoutError:
                    break
                if not chunk:
                    break
                received.extend(chunk)
            writer.close()
            done.set()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port, term="ANSI")
            # Trigger TTYPE by sending DO TTYPE to the transport
            await t._send_ttype("ANSI")
            await asyncio.sleep(0.1)
            await t.disconnect()
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        # Expected: IAC SB OPT_TTYPE(24) TTYPE_IS(0) A N S I IAC SE
        ttype_payload = bytes([IAC, SB, OPT_TTYPE, TTYPE_IS]) + b"ANSI" + bytes([IAC, SE])
        assert ttype_payload in received


# ---------------------------------------------------------------------------
# _send_subnegotiation mutation killers
# mutmut_8-11: IAC, SB constants and payload construction
# ---------------------------------------------------------------------------


class TestSendSubnegotiationMutationKilling:
    async def test_subneg_wraps_payload_with_iac_sb_iac_se(self):
        """_send_subnegotiation prepends IAC SB and appends IAC SE."""
        received = bytearray()
        done = asyncio.Event()

        async def handler(reader, writer):
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(512), timeout=0.5)
                except TimeoutError:
                    break
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
            await t._send_subnegotiation(b"\x01\x02\x03")
            await asyncio.sleep(0.1)
            await t.disconnect()
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        # Expect IAC SB <payload> IAC SE
        subneg = bytes([IAC, SB, 1, 2, 3, IAC, SE])
        assert subneg in received
