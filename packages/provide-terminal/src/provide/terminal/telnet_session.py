#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TelnetSession — telnet transport + pyte emulator satisfying the Session protocol.

Combines :class:`~provide.terminal.transports.telnet_transport.TelnetTransport`
(full RFC 854 with IAC negotiation, NAWS, TTYPE) with
:class:`~provide.terminal.emulator.TerminalEmulator` to provide a ready-to-use
:class:`~provide.terminal.io.Session`-compliant object.

Requires the ``emulator`` extra::

    pip install 'provide-terminal[emulator]'

Example::

    session = await connect_telnet("localhost", 2102)
    snap = session.snapshot()
    print(snap["screen"])
    await session.send("Hello\\r")
    await session.close()
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from provide.terminal.emulator import TerminalEmulator
from provide.terminal.transports.telnet_transport import TelnetTransport


async def connect_telnet(
    host: str,
    port: int,
    *,
    cols: int = 80,
    rows: int = 25,
    term: str = "ANSI",
    connect_timeout: float = 30.0,
) -> TelnetSession:
    """Connect to a telnet server and return a Session-protocol-compliant object.

    Uses :class:`TelnetTransport` for proper RFC 854 negotiation (IAC, NAWS,
    TTYPE) so it works with BBS servers that require telnet handshakes.

    Args:
        host: Hostname or IP address.
        port: TCP port number.
        cols: Terminal width (default 80).
        rows: Terminal height (default 25).
        term: Terminal type string (default ``"ANSI"``).
        connect_timeout: TCP connect timeout in seconds.

    Returns:
        A :class:`TelnetSession` that satisfies :class:`~provide.terminal.io.Session`.
    """
    session = TelnetSession(host, port, cols=cols, rows=rows, term=term, connect_timeout=connect_timeout)
    await session.connect()
    return session


class TelnetSession:
    """Telnet transport with pyte terminal emulation.

    Satisfies the :class:`~provide.terminal.io.Session` protocol:
    ``snapshot()``, ``send()``, ``wait_for_update()``.

    Uses :class:`TelnetTransport` (not raw :class:`TelnetClient`) for full
    RFC 854 IAC negotiation — required by TWGS and other BBS servers.

    Use :func:`connect_telnet` for a convenient factory, or construct
    directly and call :meth:`connect`.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        cols: int = 80,
        rows: int = 25,
        term: str = "ANSI",
        connect_timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._cols = cols
        self._rows = rows
        self._term = term
        self._connect_timeout = connect_timeout
        # Public read-only accessors for diagnostics and error messages
        self.host = host
        self.port = port
        self._transport = TelnetTransport()
        self._emulator = TerminalEmulator(cols, rows)
        self._read_task: asyncio.Task[None] | None = None
        self._update_event = asyncio.Event()
        self._connected = False
        self._change_seq: int = 0

    async def connect(self) -> None:
        """Open the TCP connection with IAC negotiation and start the background reader."""
        await self._transport.connect(
            self._host, self._port,
            cols=self._cols, rows=self._rows, term=self._term,
            timeout=self._connect_timeout,
        )
        self._connected = True
        self._read_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        """Close the connection and stop the background reader."""
        self._connected = False
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None
        await self._transport.disconnect()

    async def __aenter__(self) -> TelnetSession:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ── Session protocol ──────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return the current emulated screen state."""
        return self._emulator.get_snapshot()

    async def send(self, data: str) -> None:
        """Send a string to the telnet server."""
        await self._transport.send(data.encode("cp437", errors="replace"))

    async def wait_for_update(self, *, timeout_ms: int, since: int | None = None) -> bool:  # noqa: ARG002
        """Wait until new bytes arrive from the server, or timeout.

        Args:
            timeout_ms: Maximum wait time in milliseconds.
            since: Ignored (kept for protocol compatibility).

        Returns:
            ``True`` if new data arrived, ``False`` on timeout.
        """
        self._update_event.clear()
        try:
            await asyncio.wait_for(self._update_event.wait(), timeout=timeout_ms / 1000.0)
            return True
        except TimeoutError:
            return False

    def is_connected(self) -> bool:
        """Return ``True`` if the session is connected."""
        return self._connected

    def screen_change_seq(self) -> int:
        """Return a monotonic counter that increments on each screen update.

        Capture this *before* sending input, then pass the value to
        :meth:`wait_for_screen_change` to avoid reading stale screen data.
        """
        return self._change_seq

    # Alias used by some callers.
    update_seq = screen_change_seq

    async def wait_for_screen_change(self, *, timeout_ms: int = 5000, since: int | None = None) -> bool:
        """Wait until the screen updates beyond *since*, or timeout.

        Args:
            timeout_ms: Maximum wait time in milliseconds.
            since: Sequence number from :meth:`screen_change_seq`.
                If ``None``, waits for any next update.

        Returns:
            ``True`` if the screen changed, ``False`` on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
        while True:
            if since is not None and self._change_seq > since:
                return True
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            self._update_event.clear()
            try:
                await asyncio.wait_for(self._update_event.wait(), timeout=remaining)
            except TimeoutError:
                return self._change_seq > (since or 0)

    # ── Internal ──────────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        """Background task: read from transport (IAC-stripped), feed into emulator."""
        try:
            while self._connected:
                data = await self._transport.receive(4096, timeout_ms=500)
                if data:
                    self._emulator.process(data)
                    self._change_seq += 1
                    self._update_event.set()
        except (asyncio.CancelledError, ConnectionResetError, OSError, ConnectionError):
            self._connected = False
