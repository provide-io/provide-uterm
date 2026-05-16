#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for transports/telnet_transport.py."""

from __future__ import annotations

import asyncio

import pytest

from provide.uterm.transports.telnet import TelnetTransport
from provide.uterm.transports.telnet_transport import (
    IAC,
    OPT_NAWS,
    SB,
    SE,
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


# (TestReceiveMutationKilling, TestParseTelnetBuffer, TestSendNawsMutationKilling,
#  TestNegotiateMutationKilling moved to test_telnet_transport_mutation_killing_2.py)
# (TestSendTtypeMutationKilling, TestSendSubnegotiationMutationKilling moved to
#  test_telnet_transport_mutation_killing_3.py)
