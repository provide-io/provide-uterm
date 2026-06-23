#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for telnet_transport receive-buffer cap (DoS hardening).

Tests:
1. Unterminated IAC SB flood exceeding _MAX_RX_BUF_BYTES → ConnectionError.
2. Legitimate small IAC SB ... IAC SE still parses without raising (regression).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from provide.uterm.transports.telnet import TelnetTransport
from provide.uterm.transports.telnet_transport import (
    _MAX_RX_BUF_BYTES,
    IAC,
    SB,
    SE,
)

# ---------------------------------------------------------------------------
# Helpers — minimal server that sends a fixed payload then closes
# ---------------------------------------------------------------------------


class _OneShotServer:
    """Send a fixed payload to the first client, then close."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._payload:
            writer.write(self._payload)
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
        # Hold the connection open until the client disconnects, so its receive()
        # can read the payload and run the rx-buffer cap check before teardown.
        # Closing immediately races receive() on slower event loops (py3.11),
        # surfacing as a BrokenPipe / "Connection lost" instead of the cap error.
        with contextlib.suppress(Exception):
            await reader.read()
        writer.close()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


# ---------------------------------------------------------------------------
# Test 1 — unterminated SB flood exceeds cap → ConnectionError
# ---------------------------------------------------------------------------


class TestRxBufCap:
    """Validate that _rx_buf is capped and raises ConnectionError on overflow."""

    async def test_unterminated_sb_raises_connection_error(self) -> None:
        """Flooding the transport with IAC SB <no IAC SE> must raise ConnectionError.

        Strategy: directly pre-fill _rx_buf with an oversized unterminated SB,
        then call receive() with a tiny extra chunk so the cap check runs on the
        now-extended, unconsumed buffer.
        """
        # Build a tiny-chunk server payload: just one plain byte so receive()
        # calls _consume_rx_buffer() and then checks the cap.
        srv = _OneShotServer(b"X")
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Pre-fill _rx_buf with a massive unterminated SB — this is the DoS
            # scenario where the upstream sends IAC SB <opt> but never IAC SE.
            # The payload size exceeds the cap.  extend() does NOT consume it
            # because _parse_telnet_buffer sees no IAC SE.
            t._rx_buf = bytearray(bytes([IAC, SB, 0x18]) + b"A" * (_MAX_RX_BUF_BYTES + 1))
            with pytest.raises(ConnectionError, match="telnet receive buffer exceeded"):
                await t.receive(4096, timeout_ms=2000)
        finally:
            # Disconnect the client first so the server handler (holding the
            # connection open) returns, then stop the server.
            with contextlib.suppress(Exception):
                await t.disconnect()
            await srv.stop()

    # -----------------------------------------------------------------------
    # Test 2 — legitimate small SB still parses cleanly (regression guard)
    # -----------------------------------------------------------------------

    async def test_legitimate_small_sb_does_not_raise(self) -> None:
        """A well-formed IAC SB TTYPE IS 'ANSI' IAC SE must not raise.

        This verifies the cap targets UNCONSUMED bytes only — a complete
        subnegotiation is consumed by _consume_rx_buffer() so _rx_buf shrinks
        back to zero before the cap fires.
        """
        # IAC SB TTYPE(24) IS(0) "ANSI" IAC SE
        ttype_sb = bytes([IAC, SB, 24, 0]) + b"ANSI" + bytes([IAC, SE])
        # Wrap with a plain-text payload after, so receive() returns something
        payload = ttype_sb + b"login:"
        srv = _OneShotServer(payload)
        port = await srv.start()
        try:
            t = TelnetTransport()
            await t.connect("127.0.0.1", port)
            # Should NOT raise; may return b"login:" (application payload only)
            result = await t.receive(4096, timeout_ms=2000)
            # We don't care about exact bytes (IAC negotiation may be mixed in)
            # but no exception must be raised and the login prompt must appear
            assert isinstance(result, bytes)
            assert b"login:" in result
        finally:
            # Disconnect the client first so the server handler (holding the
            # connection open) returns, then stop the server.
            with contextlib.suppress(Exception):
                await t.disconnect()
            await srv.stop()
