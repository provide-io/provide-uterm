#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for transports/telnet_transport.py (part 3)."""

from __future__ import annotations

import asyncio

from provide.terminal.transports.telnet import TelnetTransport
from provide.terminal.transports.telnet_transport import (
    IAC,
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
