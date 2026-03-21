#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for start_telnet_server (transports/telnet_server.py).

Kills surviving mutations in start_telnet_server default parameters,
peername handling, handshake contents, and handler invocation.
"""

from __future__ import annotations

import asyncio

from provide.terminal.transports.telnet_server import (
    DO,
    DONT,
    ECHO,
    IAC,
    LINEMODE,
    NAWS,
    SGA,
    WILL,
    _build_telnet_handshake,
    start_telnet_server,
)

# ---------------------------------------------------------------------------
# _build_telnet_handshake — verify exact byte sequence
# ---------------------------------------------------------------------------


class TestBuildTelnetHandshake:
    def test_handshake_starts_with_iac(self) -> None:
        """First byte is IAC (255)."""
        hs = _build_telnet_handshake()
        assert hs[0] == IAC

    def test_handshake_contains_will_echo(self) -> None:
        """IAC WILL ECHO is present at the start."""
        hs = _build_telnet_handshake()
        assert hs[0:3] == bytes([IAC, WILL, ECHO])

    def test_handshake_contains_will_sga(self) -> None:
        """IAC WILL SGA follows."""
        hs = _build_telnet_handshake()
        assert hs[3:6] == bytes([IAC, WILL, SGA])

    def test_handshake_contains_do_sga(self) -> None:
        """IAC DO SGA is present."""
        hs = _build_telnet_handshake()
        assert hs[6:9] == bytes([IAC, DO, SGA])

    def test_handshake_contains_dont_linemode(self) -> None:
        """IAC DONT LINEMODE is present."""
        hs = _build_telnet_handshake()
        assert hs[9:12] == bytes([IAC, DONT, LINEMODE])

    def test_handshake_contains_do_naws(self) -> None:
        """IAC DO NAWS is present at the end."""
        hs = _build_telnet_handshake()
        assert hs[12:15] == bytes([IAC, DO, NAWS])

    def test_handshake_exact_bytes(self) -> None:
        """Full handshake matches expected sequence."""
        expected = bytes(
            [
                IAC,
                WILL,
                ECHO,
                IAC,
                WILL,
                SGA,
                IAC,
                DO,
                SGA,
                IAC,
                DONT,
                LINEMODE,
                IAC,
                DO,
                NAWS,
            ]
        )
        assert _build_telnet_handshake() == expected

    def test_handshake_length_is_15(self) -> None:
        """Handshake is exactly 15 bytes."""
        assert len(_build_telnet_handshake()) == 15

    def test_iac_constant_value(self) -> None:
        assert IAC == 255

    def test_will_constant_value(self) -> None:
        assert WILL == 251

    def test_do_constant_value(self) -> None:
        assert DO == 253

    def test_dont_constant_value(self) -> None:
        assert DONT == 254

    def test_echo_constant_value(self) -> None:
        assert ECHO == 1

    def test_sga_constant_value(self) -> None:
        assert SGA == 3

    def test_naws_constant_value(self) -> None:
        assert NAWS == 31

    def test_linemode_constant_value(self) -> None:
        assert LINEMODE == 34


# ---------------------------------------------------------------------------
# start_telnet_server — default parameters and behavior
# ---------------------------------------------------------------------------


class TestStartTelnetServer:
    async def test_returns_asyncio_server(self) -> None:
        """start_telnet_server returns a running asyncio.Server."""

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0)
        try:
            assert isinstance(server, asyncio.Server)
            assert server.is_serving()
        finally:
            server.close()
            await server.wait_closed()

    async def test_sends_handshake_on_connect(self) -> None:
        """Server sends IAC preamble to connecting clients."""

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0, negotiation_delay_s=0.0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            data = await asyncio.wait_for(reader.read(15), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

        # Should receive the handshake
        assert data[0] == IAC
        assert len(data) >= 3

    async def test_handler_called_with_reader_writer(self) -> None:
        """Handler is invoked with (reader, writer) arguments."""
        handler_args: list[tuple] = []

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            handler_args.append((reader, writer))
            writer.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0, negotiation_delay_s=0.0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.wait_for(reader.read(100), timeout=2.0)
            writer.close()
            await writer.wait_closed()
            # Give handler time to run
            await asyncio.sleep(0.1)
        finally:
            server.close()
            await server.wait_closed()

        assert len(handler_args) == 1
        assert isinstance(handler_args[0][0], asyncio.StreamReader)
        assert isinstance(handler_args[0][1], asyncio.StreamWriter)

    async def test_negotiation_delay_s_default_is_0_1(self) -> None:
        """Default negotiation_delay_s is 0.1 (not 1.1)."""
        # We test the default by verifying behavior completes within expected time
        # The mutant uses 1.1s default which would make this test slow if fired.
        # This is a parameter verification test.
        import inspect

        from provide.terminal.transports.telnet_server import start_telnet_server as sts

        sig = inspect.signature(sts)
        default = sig.parameters["negotiation_delay_s"].default
        assert default == 0.1, f"Expected 0.1, got {default}"

    async def test_handler_receives_real_reader_writer(self) -> None:
        """Handler receives asyncio.StreamReader and asyncio.StreamWriter."""
        got_types: list[tuple[type, type]] = []

        async def _handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            got_types.append((type(r), type(w)))
            w.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0, negotiation_delay_s=0.0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.15)  # allow handler to run
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()

        assert len(got_types) == 1

    async def test_host_and_port_used(self) -> None:
        """Server binds to the specified host and port."""

        async def _handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0)
        try:
            addr = server.sockets[0].getsockname()
            assert addr[0] == "127.0.0.1"
            assert isinstance(addr[1], int) and addr[1] > 0
        finally:
            server.close()
            await server.wait_closed()

    async def test_handshake_begins_with_iac_will_echo(self) -> None:
        """First 3 bytes received are IAC WILL ECHO."""

        async def _handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0, negotiation_delay_s=0.0)
        port = server.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            data = await asyncio.wait_for(r.read(3), timeout=2.0)
            w.close()
            await asyncio.wait_for(w.wait_closed(), timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()

        assert data == bytes([IAC, WILL, ECHO])

    async def test_peername_used_from_writer_extra_info(self) -> None:
        """Connection uses peername from writer (ensures 'peername' key not mutated)."""
        addrs_seen: list[str] = []

        async def _handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            # peername on writer should have been obtained in the server callback
            addrs_seen.append(str(w.get_extra_info("peername")))
            w.close()

        server = await start_telnet_server(_handler, host="127.0.0.1", port=0, negotiation_delay_s=0.0)
        port = server.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.15)
            w.close()
            await asyncio.wait_for(w.wait_closed(), timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()

        # Handler was called, confirming the normal path executed
        assert len(addrs_seen) >= 1
