#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TelnetTransport — full RFC 854 client implementing ConnectionTransport."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter

logger = get_logger(__name__)
# ---------------------------------------------------------------------------
# Telnet protocol constants (local copies to avoid circular imports)
# ---------------------------------------------------------------------------

IAC: int = 255
WILL: int = 251
WONT: int = 252
DO: int = 253
DONT: int = 254
SB: int = 250
SE: int = 240
ECHO: int = 1
SGA: int = 3
NAWS: int = 31
OPT_TTYPE: int = 24
TTYPE_IS: int = 0

# Default connection timeout in seconds
_DEFAULT_CONNECT_TIMEOUT_S: float = 30.0

# Telnet option codes
OPT_BINARY: int = 0
OPT_ECHO: int = ECHO
OPT_SGA_OPT: int = SGA
OPT_NAWS: int = NAWS


class TelnetTransport:
    """Full RFC 854 telnet client implementing the ConnectionTransport interface.

    Unlike :class:`TelnetClient`, this class:

    - Implements :class:`~provide.uterm.transports.base.ConnectionTransport`.
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
    def _find_subneg_end(buf: bytes, start: int) -> int | None:
        """Find the end of a SB...SE subnegotiation block.

        Returns the index just after SE, or None if the block is incomplete.
        """
        j = start
        while j < len(buf) - 1:
            if buf[j] == IAC and buf[j + 1] == SE:
                return j + 2
            j += 1
        return None

    @staticmethod
    def _parse_telnet_buffer(
        data: bytes | bytearray, final: bool = False
    ) -> tuple[bytes, list[tuple[str, int, int | bytes]], int]:
        """Parse complete telnet sequences from a buffer.

        Returns application payload bytes, control events, and bytes consumed.
        Trailing incomplete sequences are left unconsumed by the caller
        unless final=True.
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
                if final:
                    result.append(IAC)
                    i += 1
                    consumed = i
                break

            cmd = buf[i + 1]
            if cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(buf):
                    if final:
                        # Truncated negotiation: emit as literal data
                        result.extend(buf[i:])
                        i = len(buf)
                        consumed = i
                    break
                events.append(("negotiate", cmd, buf[i + 2]))
                i += 3
                consumed = i
                continue

            if cmd == SB:
                end = TelnetTransport._find_subneg_end(buf, i + 2)
                if end is None:
                    if final:
                        # Truncated subnegotiation: emit as literal data
                        result.extend(buf[i:])
                        i = len(buf)
                        consumed = i
                    break
                payload = buf[i + 2 : end - 2]
                events.append(("subnegotiation", 0, payload))
                i = end
                consumed = i
                continue

            if cmd == IAC:
                result.append(IAC)
                i += 2
                consumed = i
                continue

            i += 2
            consumed = i

        return bytes(result), events, consumed

    def _consume_rx_buffer(self, final: bool = False) -> tuple[bytes, list[tuple[str, int, int | bytes]]]:
        payload, events, consumed = self._parse_telnet_buffer(self._rx_buf, final=final)
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
        # Remap DEL (0x7F) → BS (0x08): xterm.js sends DEL for Backspace,
        # but many BBS/telnet servers expect BS for character deletion.
        escaped = data.replace(b"\x7f", b"\x08").replace(b"\xff", b"\xff\xff")
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
            payload, events = self._consume_rx_buffer(final=True)
            await self.disconnect()
            if payload:
                return payload
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

    def _track_negotiation_state(self, cmd: int, opt: int) -> None:
        """Record the negotiation command in the appropriate tracking set."""
        if cmd == DO:
            self._negotiated["do"].add(opt)
        elif cmd == DONT:
            self._negotiated["dont"].add(opt)
        elif cmd == WILL:
            self._negotiated["will"].add(opt)
        elif cmd == WONT:  # pragma: no branch
            self._negotiated["wont"].add(opt)

    async def _negotiate_do_response(self, opt: int) -> None:
        """Send the appropriate response to a DO command."""
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

    async def _negotiate_will_response(self, opt: int) -> None:
        """Send the appropriate response to a WILL command."""
        if opt in (OPT_ECHO, OPT_SGA_OPT, OPT_BINARY):
            await self._send_do(opt)
        else:
            await self._send_dont(opt)

    async def _negotiate(self, cmd: int, opt: int) -> None:
        if not self._writer:
            return
        self._track_negotiation_state(cmd, opt)
        try:
            if cmd == DO:
                await self._negotiate_do_response(opt)
            elif cmd == DONT:
                await self._send_wont(opt)
            elif cmd == WILL:
                await self._negotiate_will_response(opt)
            elif cmd == WONT:  # pragma: no branch
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
