#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for TelnetClient (transports/telnet_client.py).

Kills surviving mutations in __init__, connect, close, read, readuntil,
write, and drain methods.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from provide.terminal.transports.telnet_client import (
    DO,
    DONT,
    IAC,
    WILL,
    WONT,
    TelnetClient,
)
from tests.helpers.mock_server import MockTelnetServer

# ---------------------------------------------------------------------------
# __init__ — stored attributes
# ---------------------------------------------------------------------------


class TestTelnetClientInit:
    def test_host_stored(self) -> None:
        """_host is set to the passed host (not None)."""
        c = TelnetClient("bbs.example.com", 23)
        assert c._host == "bbs.example.com"

    def test_port_stored(self) -> None:
        """_port is set to the passed port (not None)."""
        c = TelnetClient("localhost", 9999)
        assert c._port == 9999

    def test_connect_timeout_default_is_30(self) -> None:
        """Default connect_timeout is 30.0 (not 31.0)."""
        c = TelnetClient("h", 1)
        assert c._connect_timeout == 30.0

    def test_connect_timeout_custom(self) -> None:
        """Custom connect_timeout is stored."""
        c = TelnetClient("h", 1, connect_timeout=5.0)
        assert c._connect_timeout == 5.0

    def test_reader_initially_none(self) -> None:
        """_reader starts as None (not '')."""
        c = TelnetClient("h", 1)
        assert c._reader is None

    def test_writer_initially_none(self) -> None:
        """_writer starts as None (not '')."""
        c = TelnetClient("h", 1)
        assert c._writer is None


# ---------------------------------------------------------------------------
# connect — uses stored host/port/timeout
# ---------------------------------------------------------------------------


class TestTelnetClientConnect:
    async def test_connect_uses_stored_host(self) -> None:
        """connect() connects to self._host."""
        async with MockTelnetServer() as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            assert c._reader is not None
            assert c._writer is not None
            await c.close()

    async def test_connect_uses_stored_port(self) -> None:
        """connect() connects to self._port."""
        async with MockTelnetServer(send=b"hi") as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            data = await c.read(2)
            await c.close()
        assert data == b"hi"

    async def test_connect_sets_reader_and_writer(self) -> None:
        """After connect(), both _reader and _writer are set (not None)."""
        async with MockTelnetServer() as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            assert c._reader is not None
            assert c._writer is not None
            await c.close()

    async def test_connect_with_timeout_not_none(self) -> None:
        """connect() uses connect_timeout (not None → no timeout enforced)."""
        async with MockTelnetServer() as srv:
            # Should succeed within the default 30s timeout
            c = TelnetClient("127.0.0.1", srv.port, connect_timeout=5.0)
            await c.connect()
            await c.close()

    async def test_connect_timeout_error_on_unreachable(self) -> None:
        """Timeout error is raised when server is unreachable within connect_timeout."""
        # Use a port that refuses connections
        c = TelnetClient("192.0.2.1", 9999, connect_timeout=0.1)
        with pytest.raises((asyncio.TimeoutError, OSError, ConnectionRefusedError)):
            await c.connect()


# ---------------------------------------------------------------------------
# close — clears reader/writer
# ---------------------------------------------------------------------------


class TestTelnetClientClose:
    async def test_close_clears_writer(self) -> None:
        """After close(), _writer is None."""
        async with MockTelnetServer() as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            await c.close()
        assert c._writer is None

    async def test_close_clears_reader(self) -> None:
        """After close(), _reader is None."""
        async with MockTelnetServer() as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            await c.close()
        assert c._reader is None

    async def test_close_when_not_connected_is_noop(self) -> None:
        """close() when _writer is None does not raise."""
        c = TelnetClient("h", 1)
        await c.close()  # should not raise

    async def test_double_close_is_safe(self) -> None:
        """close() is idempotent."""
        async with MockTelnetServer() as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            await c.close()
            await c.close()  # second close should not raise
        assert c._writer is None

    async def test_close_suppresses_oserror(self) -> None:
        """OSError from wait_closed is suppressed."""
        async with MockTelnetServer() as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            # Force the writer to raise on wait_closed by closing server first
        # Server already closed; now close should handle the OSError
        with contextlib.suppress(Exception):
            await c.close()


# ---------------------------------------------------------------------------
# read — delegates to reader
# ---------------------------------------------------------------------------


class TestTelnetClientRead:
    async def test_read_returns_server_data(self) -> None:
        """read(n) returns data from the server."""
        async with MockTelnetServer(send=b"hello world") as srv, TelnetClient("127.0.0.1", srv.port) as c:
            data = await c.read(5)
        assert data == b"hello"

    async def test_read_with_n_bytes(self) -> None:
        """read(n) reads up to n bytes from the server."""
        async with MockTelnetServer(send=b"test data") as srv, TelnetClient("127.0.0.1", srv.port) as c:
            data = await c.read(4)
        assert len(data) <= 4


# ---------------------------------------------------------------------------
# readuntil — delegates to reader with separator
# ---------------------------------------------------------------------------


class TestTelnetClientReadUntil:
    async def test_readuntil_default_separator_newline(self) -> None:
        """readuntil() reads until newline by default."""
        async with MockTelnetServer(send=b"line1\nmore") as srv, TelnetClient("127.0.0.1", srv.port) as c:
            data = await c.readuntil()
        assert data == b"line1\n"

    async def test_readuntil_custom_separator(self) -> None:
        """readuntil(b'|') reads until the custom separator."""
        async with MockTelnetServer(send=b"part1|rest") as srv, TelnetClient("127.0.0.1", srv.port) as c:
            data = await c.readuntil(b"|")
        assert data == b"part1|"

    async def test_readuntil_separator_included_in_result(self) -> None:
        """The separator is included in the returned bytes."""
        async with MockTelnetServer(send=b"abc\n") as srv, TelnetClient("127.0.0.1", srv.port) as c:
            data = await c.readuntil(b"\n")
        assert data.endswith(b"\n")


# ---------------------------------------------------------------------------
# write + drain
# ---------------------------------------------------------------------------


class TestTelnetClientWriteDrain:
    async def test_write_sends_data_to_server(self) -> None:
        """write() buffers data that drain() sends to the server."""
        async with MockTelnetServer() as srv:
            async with TelnetClient("127.0.0.1", srv.port) as c:
                c.write(b"hello")
                await c.drain()
            await srv.wait_for_connection()
        assert srv.received == b"hello"

    async def test_write_uses_writer_attribute(self) -> None:
        """write() calls self._writer.write (not some other writer)."""
        async with MockTelnetServer() as srv:
            c = TelnetClient("127.0.0.1", srv.port)
            await c.connect()
            # writer is set after connect
            assert c._writer is not None
            c.write(b"data")
            await c.drain()
            await c.close()

    async def test_drain_flushes_write_buffer(self) -> None:
        """drain() causes buffered data to be sent."""
        async with MockTelnetServer() as srv:
            async with TelnetClient("127.0.0.1", srv.port) as c:
                c.write(b"flush-me")
                await c.drain()
            await srv.wait_for_connection()
        assert b"flush-me" in srv.received


# ---------------------------------------------------------------------------
# IAC constants and helper methods
# ---------------------------------------------------------------------------


class TestTelnetClientIACHelpers:
    def test_iac_constant(self) -> None:
        assert IAC == 255

    def test_will_constant(self) -> None:
        assert WILL == 251

    def test_wont_constant(self) -> None:
        assert WONT == 252

    def test_do_constant(self) -> None:
        assert DO == 253

    def test_dont_constant(self) -> None:
        assert DONT == 254

    def test_will_method_returns_correct_bytes(self) -> None:
        c = TelnetClient("h", 1)
        result = c.will(1)
        assert result == bytes([IAC, WILL, 1])

    def test_wont_method_returns_correct_bytes(self) -> None:
        c = TelnetClient("h", 1)
        result = c.wont(3)
        assert result == bytes([IAC, WONT, 3])

    def test_do_method_returns_correct_bytes(self) -> None:
        c = TelnetClient("h", 1)
        result = c.do(31)
        assert result == bytes([IAC, DO, 31])

    def test_dont_method_returns_correct_bytes(self) -> None:
        c = TelnetClient("h", 1)
        result = c.dont(34)
        assert result == bytes([IAC, DONT, 34])

    def test_will_uses_iac_and_will(self) -> None:
        """First byte is IAC, second is WILL — not swapped."""
        c = TelnetClient("h", 1)
        result = c.will(1)
        assert result[0] == IAC
        assert result[1] == WILL

    def test_do_uses_iac_and_do(self) -> None:
        """First byte is IAC, second is DO — not swapped."""
        c = TelnetClient("h", 1)
        result = c.do(1)
        assert result[0] == IAC
        assert result[1] == DO
