#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TelnetSession — telnet transport + pyte emulator satisfying the Session protocol.

Combines :class:`~provide.uterm.transports.telnet_transport.TelnetTransport`
(full RFC 854 with IAC negotiation, NAWS, TTYPE) with
:class:`~provide.uterm.emulator.TerminalEmulator` to provide a ready-to-use
:class:`~provide.uterm.io.Session`-compliant object.

Most of the behavior is inherited from
:class:`~provide.uterm.transport_session.TransportSession`; this module only
supplies the telnet transport, the CP437 send encoding, and the telnet-specific
connect arguments (NAWS geometry, terminal type, connect timeout).

Requires the ``emulator`` extra::

    pip install 'provide-uterm[emulator]'

Example::

    session = await connect_telnet("localhost", 2102)
    snap = session.snapshot()
    print(snap["screen"])
    await session.send("Hello\\r")
    await session.close()
"""

from __future__ import annotations

from provide.uterm.transport_session import TransportSession

# ``TelnetTransport`` is imported here (not only inside the base) because it is
# the transport constructed in ``TelnetSession.__init__`` — so patching
# ``provide.uterm.telnet_session.TelnetTransport`` intercepts construction, the
# target downstream test suites (e.g. uwarp-space) rely on.
from provide.uterm.transports.telnet_transport import TelnetTransport

__all__ = ["TelnetSession", "TelnetTransport", "connect_telnet"]


async def connect_telnet(
    host: str,
    port: int,
    *,
    cols: int = 80,
    rows: int = 25,
    term: str = "ANSI",
    connect_timeout: float = 30.0,
    receive_encoding: str = "cp437",
    control_frames: bool = False,
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
        receive_encoding: Codec used to decode incoming terminal bytes.
            Defaults to CP437 for BBS compatibility.
        control_frames: When ``True``, inline DLE/STX control frames are
            parsed out of the stream and routed to
            ``session.add_control_frame_watch(...)`` instead of appearing as
            literal text on screen. Off by default — every byte goes straight
            to the emulator unmodified, matching a plain telnet client.

    Returns:
        A :class:`TelnetSession` that satisfies :class:`~provide.uterm.io.Session`.

    Tip:
        To tap raw bytes from the terminal stream, call
        ``session.add_watch(...)`` on the returned session; do not monkey-patch
        the emulator internals.
    """
    session = TelnetSession(
        host,
        port,
        cols=cols,
        rows=rows,
        term=term,
        connect_timeout=connect_timeout,
        receive_encoding=receive_encoding,
        control_frames=control_frames,
    )
    await session.connect()
    return session


class TelnetSession(TransportSession):
    """Telnet transport with pyte terminal emulation.

    Satisfies the :class:`~provide.uterm.io.Session` protocol:
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
        receive_encoding: str = "cp437",
        control_frames: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._term = term
        self._connect_timeout = connect_timeout
        # Public read-only accessors for diagnostics and error messages
        self.host = host
        self.port = port
        # CP437 send encoding preserves the high-byte / ANSI conventions that
        # BBS servers expect on the wire.
        super().__init__(
            TelnetTransport(),
            cols=cols,
            rows=rows,
            send_encoding="cp437",
            receive_encoding=receive_encoding,
            control_frames=control_frames,
        )

    async def _connect_transport(self) -> None:
        """Open the TCP connection with full IAC negotiation (NAWS/TTYPE)."""
        await self._transport.connect(
            self._host,
            self._port,
            cols=self._cols,
            rows=self._rows,
            term=self._term,
            timeout=self._connect_timeout,
        )
