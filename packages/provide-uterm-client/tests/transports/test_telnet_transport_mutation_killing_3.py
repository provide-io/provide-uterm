#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for transports/telnet_transport.py (part 3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.transports.telnet import TelnetTransport
from provide.uterm.transports.telnet_transport import (
    DO,
    IAC,
    OPT_BINARY,
    OPT_TTYPE,
    SB,
    SE,
    TTYPE_IS,
)

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


# ---------------------------------------------------------------------------
# Exception paths in send/receive/disconnect/_negotiate (formerly pragma'd)
# ---------------------------------------------------------------------------


class TestTelnetTransportConnectionErrorPaths:
    """Exercise the previously-pragma'd ConnectionResetError/BrokenPipeError
    branches in disconnect(), send(), receive(), and _negotiate() by injecting
    mock streams that raise on demand."""

    def _make_transport_with_mocks(
        self,
        reader: MagicMock | None = None,
        writer: MagicMock | None = None,
    ) -> TelnetTransport:
        t = TelnetTransport()
        if writer is None:
            writer = MagicMock()
            writer.is_closing = MagicMock(return_value=False)
            writer.close = MagicMock()
            writer.wait_closed = AsyncMock()
            writer.write = MagicMock()
            writer.drain = AsyncMock()
        if reader is None:
            reader = MagicMock()
            reader.read = AsyncMock(return_value=b"")
        t._writer = writer
        t._reader = reader
        return t

    async def test_disconnect_suppresses_connection_reset(self) -> None:
        """disconnect() swallows ConnectionResetError from wait_closed()."""
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=ConnectionResetError("peer closed"))
        t = self._make_transport_with_mocks(writer=writer)
        await t.disconnect()  # must not raise
        assert t._writer is None

    async def test_disconnect_suppresses_broken_pipe(self) -> None:
        """disconnect() swallows BrokenPipeError from wait_closed()."""
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=BrokenPipeError("epipe"))
        t = self._make_transport_with_mocks(writer=writer)
        await t.disconnect()
        assert t._writer is None

    async def test_disconnect_suppresses_runtime_error(self) -> None:
        """disconnect() swallows RuntimeError from wait_closed()."""
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=RuntimeError("loop closed"))
        t = self._make_transport_with_mocks(writer=writer)
        await t.disconnect()
        assert t._writer is None

    async def test_send_raises_connection_error_on_reset(self) -> None:
        """send() reraises ConnectionResetError from writer as ConnectionError."""
        writer = MagicMock()
        writer.is_closing = MagicMock(return_value=False)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock(side_effect=ConnectionResetError("peer reset"))
        t = self._make_transport_with_mocks(writer=writer)
        with pytest.raises(ConnectionError, match="Send failed"):
            await t.send(b"hello")
        assert t._writer is None  # disconnect ran

    async def test_send_raises_connection_error_on_broken_pipe(self) -> None:
        """send() reraises BrokenPipeError as ConnectionError."""
        writer = MagicMock()
        writer.is_closing = MagicMock(return_value=False)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock(side_effect=BrokenPipeError("epipe"))
        t = self._make_transport_with_mocks(writer=writer)
        with pytest.raises(ConnectionError, match="Send failed"):
            await t.send(b"hello")

    async def test_receive_raises_connection_error_on_reset(self) -> None:
        """receive() reraises ConnectionResetError as ConnectionError."""
        reader = MagicMock()
        reader.read = AsyncMock(side_effect=ConnectionResetError("peer reset"))
        t = self._make_transport_with_mocks(reader=reader)
        with pytest.raises(ConnectionError, match="Connection lost"):
            await t.receive(64, 1000)
        assert t._writer is None  # disconnect ran

    async def test_receive_raises_connection_error_on_broken_pipe(self) -> None:
        """receive() reraises BrokenPipeError as ConnectionError."""
        reader = MagicMock()
        reader.read = AsyncMock(side_effect=BrokenPipeError("epipe"))
        t = self._make_transport_with_mocks(reader=reader)
        with pytest.raises(ConnectionError, match="Connection lost"):
            await t.receive(64, 1000)

    async def test_receive_empty_chunk_signals_remote_close(self) -> None:
        """receive() with empty chunk and empty buffer raises ConnectionError."""
        reader = MagicMock()
        reader.read = AsyncMock(return_value=b"")
        t = self._make_transport_with_mocks(reader=reader)
        with pytest.raises(ConnectionError, match="Connection closed by remote"):
            await t.receive(64, 1000)
        assert t._writer is None

    async def test_receive_empty_chunk_returns_buffered_payload(self) -> None:
        """receive() returns whatever payload remains buffered when remote EOFs."""
        reader = MagicMock()
        reader.read = AsyncMock(return_value=b"")
        t = self._make_transport_with_mocks(reader=reader)
        # Pre-seed buffer with raw bytes (no IAC) so _consume_rx_buffer
        # returns a non-empty payload on the final flush.
        t._rx_buf.extend(b"buffered-data")
        out = await t.receive(64, 1000)
        assert out == b"buffered-data"

    async def test_negotiate_suppresses_connection_reset(self) -> None:
        """_negotiate() swallows ConnectionResetError raised by writer.write."""
        writer = MagicMock()
        writer.is_closing = MagicMock(return_value=False)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        # Make writer.write raise; _send_cmd writes synchronously before drain.
        writer.write = MagicMock(side_effect=ConnectionResetError("peer reset"))
        writer.drain = AsyncMock()
        t = self._make_transport_with_mocks(writer=writer)
        # DO BINARY → _negotiate calls _negotiate_do_response → _send_will → _send_cmd
        await t._negotiate(DO, OPT_BINARY)  # must not raise

    async def test_negotiate_suppresses_broken_pipe(self) -> None:
        """_negotiate() swallows BrokenPipeError raised by writer.write."""
        writer = MagicMock()
        writer.is_closing = MagicMock(return_value=False)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.write = MagicMock(side_effect=BrokenPipeError("epipe"))
        writer.drain = AsyncMock()
        t = self._make_transport_with_mocks(writer=writer)
        await t._negotiate(DO, OPT_BINARY)  # must not raise
