#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Reusable mock TCP server for testing telnet transports."""

from __future__ import annotations

import asyncio
import socket


class MockTelnetServer:
    """Async context manager that starts a real TCP server on a random port.

    Accepts one connection, optionally sends a predefined byte sequence,
    and collects received bytes for assertion.

    Example::

        async with MockTelnetServer(send=b"hello") as srv:
            async with TelnetClient("127.0.0.1", srv.port) as client:
                data = await client.read(5)
        assert data == b"hello"
        assert srv.received == b""
    """

    def __init__(self, send: bytes = b"") -> None:
        self._send = send
        self._port: int = 0
        self._received = bytearray()
        self._server: asyncio.Server | None = None
        self._connection_event = asyncio.Event()

    async def _handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._send:
            writer.write(self._send)
            await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            self._received.extend(chunk)
        writer.close()
        self._connection_event.set()

    async def __aenter__(self) -> MockTelnetServer:
        # Pick a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self._port = s.getsockname()[1]

        self._server = await asyncio.start_server(self._handler, "127.0.0.1", self._port)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def port(self) -> int:
        """The TCP port this server is listening on."""
        return self._port

    @property
    def received(self) -> bytes:
        """All bytes received from clients so far."""
        return bytes(self._received)

    async def wait_for_connection(self, timeout: float = 2.0) -> None:
        """Wait until a client has connected and disconnected."""
        await asyncio.wait_for(self._connection_event.wait(), timeout=timeout)
