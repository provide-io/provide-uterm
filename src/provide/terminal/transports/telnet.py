#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Telnet transport for provide-terminal.

Provides:
- :class:`TelnetClient` — thin client wrapper around ``asyncio.open_connection``
  with IAC constants and negotiation helpers.
- :class:`TelnetTransport` — full RFC 854 client implementing
  :class:`~provide.terminal.transports.base.ConnectionTransport`.
- Telnet protocol constants: ``IAC``, ``WILL``, ``WONT``, ``DO``, ``DONT``, ``SB``, ``SE``.
- :func:`start_telnet_server` re-exported from
  :mod:`~provide.terminal.transports.telnet_server` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from provide.terminal.transports.telnet_server import start_telnet_server as start_telnet_server

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telnet protocol constants
# ---------------------------------------------------------------------------

IAC: int = 255  # Interpret As Command
WILL: int = 251  # Will perform option
WONT: int = 252  # Won't perform option
DO: int = 253  # Do perform option
DONT: int = 254  # Don't perform option
SB: int = 250  # Sub-negotiation Begin
SE: int = 240  # Sub-negotiation End

# Telnet options
ECHO: int = 1  # Echo
SGA: int = 3  # Suppress Go Ahead
NAWS: int = 31  # Negotiate About Window Size
LINEMODE: int = 34  # Linemode

# Terminal type subnegotiation
OPT_TTYPE: int = 24
TTYPE_IS: int = 0

# ---------------------------------------------------------------------------
# Client-mode
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Full RFC 854 client — ConnectionTransport implementation
# ---------------------------------------------------------------------------

# Default connection timeout in seconds
_DEFAULT_CONNECT_TIMEOUT_S: float = 30.0

# Telnet option codes (aliases for TelnetTransport use)
OPT_BINARY: int = 0
OPT_ECHO: int = ECHO
OPT_SGA_OPT: int = SGA
OPT_NAWS: int = NAWS


class TelnetTransport:
    """Full RFC 854 telnet client implementing the ConnectionTransport interface.

    Unlike :class:`TelnetClient`, this class:

    - Implements :class:`~provide.terminal.transports.base.ConnectionTransport`.
    - IAC-escapes ``0xFF`` bytes in outgoing data (binary safety).
    - Handles full option negotiation: ECHO, SGA, NAWS, TTYPE.
    - Buffers incoming data and strips IAC sequences before returning payload.

    Example::

        transport = TelnetTransport()
        await transport.connect("bbs.example.com", 23, cols=80, rows=25)
        await transport.send(b"hello\\r")
        data = await transport.receive(4096, timeout_ms=5000)
        await transport.disconnect()
    """

    def __init__(self) -> None:
        self._reader: StreamReader | None = None
        self._writer: StreamWriter | None = None
        self._negotiated: dict[str, set[int]] = {"do": set(), "dont": set(), "will": set(), "wont": set()}
        self._rx_buf = bytearray()
        self._cols: int = 80
        self._rows: int = 25
        self._term: str = "ANSI"
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _parse_telnet_buffer(data: bytes | bytearray) -> tuple[bytes, list[tuple[str, int, int | bytes]], int]:
        """Parse complete telnet sequences from a buffer.

        Returns application payload bytes, control events, and bytes consumed.
        Trailing incomplete sequences are left unconsumed by the caller.
        """
        result = bytearray()
        events: list[tuple[str, int, int | bytes]] = []
        i = 0
        consumed = 0
        buf = bytes(data)

        while i < len(buf):
            if buf[i] != IAC:
                result.append(buf[i])
                i += 1
                consumed = i
                continue

            if i + 1 >= len(buf):
                break

            cmd = buf[i + 1]
            if cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(buf):
                    break
                events.append(("negotiate", cmd, buf[i + 2]))
                i += 3
                consumed = i
                continue

            if cmd == SB:
                j = i + 2
                while j < len(buf) - 1:
                    if buf[j] == IAC and buf[j + 1] == SE:
                        payload = buf[i + 2 : j]
                        events.append(("subnegotiation", 0, payload))
                        i = j + 2
                        consumed = i
                        break
                    j += 1
                else:
                    break
                continue

            if cmd == IAC:
                result.append(IAC)
                i += 2
                consumed = i
                continue

            i += 2
            consumed = i

        return bytes(result), events, consumed

    def _consume_rx_buffer(self) -> tuple[bytes, list[tuple[str, int, int | bytes]]]:
        payload, events, consumed = self._parse_telnet_buffer(self._rx_buf)
        if consumed:
            del self._rx_buf[:consumed]
        return payload, events

    async def connect(
        self,
        host: str,
        port: int,
        cols: int = 80,
        rows: int = 25,
        term: str = "ANSI",
        timeout: float = _DEFAULT_CONNECT_TIMEOUT_S,
        **_kwargs: Any,
    ) -> None:
        """Open a telnet connection.

        Args:
            host: Remote hostname or IP.
            port: Remote port.
            cols: Terminal columns for NAWS.
            rows: Terminal rows for NAWS.
            term: Terminal type string (e.g. ``"ANSI"``).
            timeout: Connection timeout in seconds.

        Raises:
            ConnectionError: If the connection attempt fails.
        """
        if self._writer:
            await self.disconnect()
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        except Exception as exc:
            raise ConnectionError(f"Failed to connect to {host}:{port}") from exc

        self._cols = cols
        self._rows = rows
        self._term = term

        await self._send_will(OPT_BINARY)
        await self._send_will(OPT_SGA_OPT)
        logger.debug("telnet_transport connected host=%s port=%d", host, port)

    async def disconnect(self) -> None:
        """Close the connection."""
        if not self._writer:
            return
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, RuntimeError):  # pragma: no cover
            pass
        finally:
            for t in list(self._tasks):
                t.cancel()
            self._tasks.clear()
            self._writer = None
            self._reader = None
            self._rx_buf.clear()
            self._negotiated = {"do": set(), "dont": set(), "will": set(), "wont": set()}

    async def send(self, data: bytes) -> None:
        """Send bytes with IAC escaping (RFC 854: ``0xFF`` → ``0xFF 0xFF``).

        Args:
            data: Raw bytes to send.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._writer:
            raise ConnectionError("Not connected")
        escaped = data.replace(b"\xff", b"\xff\xff")
        try:
            self._writer.write(escaped)
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:  # pragma: no cover
            await self.disconnect()
            raise ConnectionError("Send failed") from exc

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        """Receive bytes, stripping IAC sequences.

        Args:
            max_bytes: Max bytes to read.
            timeout_ms: Read timeout in milliseconds (0 means return immediately on no data).

        Returns:
            Application-layer bytes (may be empty on timeout).

        Raises:
            ConnectionError: If not connected or connection closed.
        """
        if not self._reader:
            raise ConnectionError("Not connected")
        try:
            chunk = await asyncio.wait_for(self._reader.read(max_bytes), timeout=timeout_ms / 1000)
        except TimeoutError:
            return b""
        except (ConnectionResetError, BrokenPipeError) as exc:  # pragma: no cover
            await self.disconnect()
            raise ConnectionError("Connection lost") from exc

        if not chunk:  # pragma: no cover
            await self.disconnect()
            raise ConnectionError("Connection closed by remote")

        self._rx_buf.extend(chunk)
        payload, events = self._consume_rx_buffer()
        for event_type, cmd, opt_or_payload in events:
            if event_type == "negotiate":
                task = asyncio.create_task(self._negotiate(cmd, int(opt_or_payload)))
            else:
                task = asyncio.create_task(self._handle_subnegotiation(bytes(opt_or_payload)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return payload

    def is_connected(self) -> bool:
        """Return ``True`` if the connection is active."""
        return self._writer is not None and not self._writer.is_closing()

    async def set_size(self, cols: int, rows: int) -> None:
        """Update terminal size and send NAWS subnegotiation.

        Args:
            cols: New column count.
            rows: New row count.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._writer:
            raise ConnectionError("Not connected")
        self._cols = cols
        self._rows = rows
        await self._send_naws(cols, rows)

    async def _negotiate(self, cmd: int, opt: int) -> None:
        if not self._writer:
            return
        if cmd == DO:
            self._negotiated["do"].add(opt)
        elif cmd == DONT:
            self._negotiated["dont"].add(opt)
        elif cmd == WILL:
            self._negotiated["will"].add(opt)
        elif cmd == WONT:
            self._negotiated["wont"].add(opt)

        try:
            if cmd == DO:
                if opt in (OPT_BINARY, OPT_SGA_OPT):
                    await self._send_will(opt)
                elif opt == OPT_NAWS:
                    await self._send_will(opt)
                    await self._send_naws(self._cols, self._rows)
                elif opt == OPT_TTYPE:
                    await self._send_will(opt)
                    await self._send_ttype(self._term)
                else:
                    await self._send_wont(opt)
            elif cmd == DONT:
                await self._send_wont(opt)
            elif cmd == WILL:
                if opt in (OPT_ECHO, OPT_SGA_OPT, OPT_BINARY):
                    await self._send_do(opt)
                else:
                    await self._send_dont(opt)
            elif cmd == WONT:
                await self._send_dont(opt)
        except (ConnectionResetError, BrokenPipeError):  # pragma: no cover
            pass

    async def _handle_subnegotiation(self, sub: bytes) -> None:
        if not sub or not self._writer:
            return
        if sub[0] == OPT_TTYPE and len(sub) > 1 and sub[1] == 1:
            await self._send_ttype(self._term)

    async def _send_cmd(self, cmd: int, opt: int) -> None:
        if not self._writer or self._writer.is_closing():
            return
        self._writer.write(bytes([IAC, cmd, opt]))
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await self._writer.drain()

    async def _send_will(self, opt: int) -> None:
        # NOTE: _negotiate tasks run concurrently; two tasks could both pass the
        # `not in` check before either adds to the set (TOCTOU).  In practice
        # this only occurs if the server sends duplicate DO/WILL for the same
        # option, which is a protocol violation.  A duplicate WILL is harmless.
        if opt not in self._negotiated["will"]:
            await self._send_cmd(WILL, opt)
            self._negotiated["will"].add(opt)

    async def _send_wont(self, opt: int) -> None:
        if opt not in self._negotiated["wont"]:
            await self._send_cmd(WONT, opt)
            self._negotiated["wont"].add(opt)

    async def _send_do(self, opt: int) -> None:
        if opt not in self._negotiated["do"]:
            await self._send_cmd(DO, opt)
            self._negotiated["do"].add(opt)

    async def _send_dont(self, opt: int) -> None:
        if opt not in self._negotiated["dont"]:
            await self._send_cmd(DONT, opt)
            self._negotiated["dont"].add(opt)

    async def _send_naws(self, cols: int, rows: int) -> None:
        if not self._writer or self._writer.is_closing():
            return
        wh = (cols >> 8) & 0xFF
        wl = cols & 0xFF
        hh = (rows >> 8) & 0xFF
        hl = rows & 0xFF
        self._writer.write(bytes([IAC, SB, OPT_NAWS, wh, wl, hh, hl, IAC, SE]))
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await self._writer.drain()

    async def _send_ttype(self, term: str) -> None:
        payload = bytes([OPT_TTYPE, TTYPE_IS]) + term.encode("ascii", errors="replace")
        await self._send_subnegotiation(payload)

    async def _send_subnegotiation(self, payload: bytes) -> None:
        if not self._writer or self._writer.is_closing():
            return
        self._writer.write(bytes([IAC, SB]) + payload + bytes([IAC, SE]))
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await self._writer.drain()
