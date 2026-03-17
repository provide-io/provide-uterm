#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TelnetClient — thin asyncio telnet client with IAC negotiation helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

# Telnet protocol constants (duplicated here to avoid circular imports)
IAC: int = 255  # Interpret As Command
WILL: int = 251  # Will perform option
WONT: int = 252  # Won't perform option
DO: int = 253  # Do perform option
DONT: int = 254  # Don't perform option


class TelnetClient:
    """Thin asyncio telnet client with IAC negotiation helpers.

    Wraps ``asyncio.open_connection`` and provides methods to read/write
    bytes and respond to telnet IAC negotiations.

    Example::

        async with TelnetClient("bbs.example.com", 23) as client:
            data = await client.read(1024)
    """

    def __init__(self, host: str, port: int, *, connect_timeout: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Open the TCP connection.

        Raises ``asyncio.TimeoutError`` if the host does not respond within
        ``connect_timeout`` seconds (default 30 s).
        """
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=self._connect_timeout,
        )
        logger.debug("telnet client connected host=%s port=%d", self._host, self._port)

    async def close(self) -> None:
        """Close the TCP connection."""
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError, ConnectionResetError):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None

    async def __aenter__(self) -> TelnetClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def read(self, n: int) -> bytes:
        """Read up to *n* bytes from the server."""
        if self._reader is None:  # pragma: no cover
            raise RuntimeError("not connected")
        return await self._reader.read(n)

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        """Read until *separator* is found."""
        if self._reader is None:  # pragma: no cover
            raise RuntimeError("not connected")
        return await self._reader.readuntil(separator)

    def write(self, data: bytes) -> None:
        """Write *data* to the server (buffered until :meth:`drain`)."""
        if self._writer is None:  # pragma: no cover
            raise RuntimeError("not connected")
        self._writer.write(data)

    async def drain(self) -> None:
        """Flush the write buffer."""
        if self._writer is None:  # pragma: no cover
            raise RuntimeError("not connected")
        await self._writer.drain()

    def will(self, option: int) -> bytes:
        """Build an IAC WILL *option* sequence."""
        return bytes([IAC, WILL, option])

    def wont(self, option: int) -> bytes:
        """Build an IAC WONT *option* sequence."""
        return bytes([IAC, WONT, option])

    def do(self, option: int) -> bytes:
        """Build an IAC DO *option* sequence."""
        return bytes([IAC, DO, option])

    def dont(self, option: int) -> bytes:
        """Build an IAC DONT *option* sequence."""
        return bytes([IAC, DONT, option])
