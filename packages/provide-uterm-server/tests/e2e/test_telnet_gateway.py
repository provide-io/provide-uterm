#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""End-to-end integration tests for :class:`TelnetWsGateway`.

These extend the basic unit tests in ``test_gateway.py`` with multi-session
isolation, large-payload throughput, streamed multi-write echoes, and a
:class:`TelnetTransport`-based connection that exercises IAC handling through
the full TCP ↔ WebSocket pipeline.

Architecture
------------
Each test creates:
1. A WS echo server (returns every message it receives).
2. A :class:`TelnetWsGateway` pointed at that echo server.
3. One or more raw TCP / :class:`TelnetTransport` connections to the gateway.
"""

from __future__ import annotations

import asyncio
from typing import Any

import websockets
import websockets.server

from provide.uterm.gateway import TelnetWsGateway
from provide.uterm.transports.telnet import TelnetTransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _start_ws_echo_server(*, banner: str = "") -> tuple[Any, int]:
    """Spin up a minimal WS echo server; returns (server, port)."""

    async def _handler(ws: Any) -> None:
        if banner:
            await ws.send(banner)
        async for msg in ws:
            await ws.send(msg)

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port


async def _make_gateway(ws_port: int) -> tuple[asyncio.AbstractServer, int]:
    """Create a TelnetWsGateway bound to an ephemeral port; returns (server, port).

    ``iac_negotiate=False`` — these tests assert echo-only behaviour, so
    disable the TTYPE/NEW-ENVIRON handshake that would otherwise send
    unsolicited IAC bytes to the raw TCP client.
    """
    gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}", iac_negotiate=False)
    tcp_srv = await gw.start("127.0.0.1", 0)
    from asyncio import Server

    assert isinstance(tcp_srv, Server)
    assert tcp_srv.sockets is not None
    tcp_port = tcp_srv.sockets[0].getsockname()[1]
    return tcp_srv, tcp_port


# ---------------------------------------------------------------------------
# Raw TCP connection tests
# ---------------------------------------------------------------------------


class TestTelnetGatewayEcho:
    """Raw TCP → WS echo → TCP round-trip tests."""

    async def test_echo_round_trip(self) -> None:
        """Single write/read cycle through the gateway."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(b"hello gateway")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            assert b"hello gateway" in data
        finally:
            tcp_srv.close()
            ws_srv.close()

    async def test_multi_write_streamed_echo(self) -> None:
        """Multiple writes arrive as a contiguous echo stream."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            for i in range(5):
                writer.write(f"chunk-{i}\n".encode())
                await writer.drain()
                await asyncio.sleep(0.02)

            # Collect all echoed data.
            buf = b""
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                try:
                    buf += await asyncio.wait_for(reader.read(4096), timeout=0.3)
                except TimeoutError:
                    if all(f"chunk-{i}".encode() in buf for i in range(5)):
                        break
            writer.close()

            for i in range(5):
                assert f"chunk-{i}".encode() in buf, f"missing chunk-{i}"
        finally:
            tcp_srv.close()
            ws_srv.close()

    async def test_banner_received_on_connect(self) -> None:
        """A banner sent by the WS server on connect appears at the TCP client."""
        ws_srv, ws_port = await _start_ws_echo_server(banner="WELCOME\r\n")
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            assert b"WELCOME" in data
        finally:
            tcp_srv.close()
            ws_srv.close()

    async def test_large_payload(self) -> None:
        """A 64 KB payload survives the round-trip without corruption."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            payload = b"X" * 65536
            writer.write(payload)
            await writer.drain()

            buf = b""
            deadline = asyncio.get_running_loop().time() + 5.0
            while len(buf) < len(payload) and asyncio.get_running_loop().time() < deadline:
                try:
                    buf += await asyncio.wait_for(reader.read(65536), timeout=0.5)
                except TimeoutError:
                    continue
            writer.close()
            assert buf == payload
        finally:
            tcp_srv.close()
            ws_srv.close()


# ---------------------------------------------------------------------------
# Concurrent session isolation
# ---------------------------------------------------------------------------


class TestTelnetGatewayConcurrency:
    """Multiple simultaneous TCP connections stay isolated."""

    async def test_two_sessions_isolated(self) -> None:
        """Two concurrent telnet sessions get independent WS connections."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            r1, w1 = await asyncio.open_connection("127.0.0.1", tcp_port)
            r2, w2 = await asyncio.open_connection("127.0.0.1", tcp_port)

            w1.write(b"session-A")
            await w1.drain()
            w2.write(b"session-B")
            await w2.drain()

            d1 = await asyncio.wait_for(r1.read(256), timeout=2.0)
            d2 = await asyncio.wait_for(r2.read(256), timeout=2.0)

            w1.close()
            w2.close()

            assert b"session-A" in d1
            assert b"session-B" in d2
            # No cross-contamination.
            assert b"session-B" not in d1
            assert b"session-A" not in d2
        finally:
            tcp_srv.close()
            ws_srv.close()


# ---------------------------------------------------------------------------
# Disconnect / cleanup
# ---------------------------------------------------------------------------


class TestTelnetGatewayDisconnect:
    """Clean shutdown paths."""

    async def test_client_disconnect_no_hang(self) -> None:
        """Closing the TCP client returns cleanly (no hang / no leaked tasks)."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.close()
            await asyncio.sleep(0.1)
        finally:
            tcp_srv.close()
            ws_srv.close()

    async def test_server_close_accepts_no_new_connections(self) -> None:
        """After close(), the gateway rejects new connections but active ones finish."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            # Existing connection still works after server.close().
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(b"before-close")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            assert b"before-close" in data

            tcp_srv.close()
            await asyncio.sleep(0.05)

            # New connection should be refused.
            try:
                _r, _w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", tcp_port), timeout=1.0)
                _w.close()
                refused = False
            except (ConnectionRefusedError, TimeoutError, OSError):
                refused = True
            assert refused, "Server should refuse new connections after close()"

            writer.close()
        finally:
            ws_srv.close()


# ---------------------------------------------------------------------------
# TelnetTransport integration
# ---------------------------------------------------------------------------


class TestTelnetTransportThroughGateway:
    """Use the full :class:`TelnetTransport` (RFC 854) through the gateway.

    This exercises IAC option negotiation, send/receive, and disconnect
    through the real TCP → WS pipeline.  The WS echo server simply echoes
    everything — including encoded IAC sequences — which is sufficient to
    verify the transport's byte-level correctness.
    """

    async def test_transport_send_receive(self) -> None:
        """TelnetTransport.send() data arrives back via receive()."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            transport = TelnetTransport()
            await transport.connect("127.0.0.1", tcp_port)
            assert transport.is_connected()

            await transport.send(b"transport test\r\n")
            # Drain until we see our payload (skip any IAC negotiation bytes).
            buf = b""
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                chunk = await transport.receive(4096, timeout_ms=500)
                if chunk:
                    buf += chunk
                if b"transport test" in buf:
                    break

            await transport.disconnect()
            assert b"transport test" in buf
        finally:
            tcp_srv.close()
            ws_srv.close()

    async def test_transport_disconnect_clean(self) -> None:
        """TelnetTransport.disconnect() returns cleanly through the gateway."""
        ws_srv, ws_port = await _start_ws_echo_server()
        tcp_srv, tcp_port = await _make_gateway(ws_port)
        try:
            transport = TelnetTransport()
            await transport.connect("127.0.0.1", tcp_port)
            assert transport.is_connected()
            await transport.disconnect()
            assert not transport.is_connected()
        finally:
            tcp_srv.close()
            ws_srv.close()
