#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocketSession — WebSocket transport + pyte emulator satisfying Session.

Combines :class:`~provide.uterm.transports.ws_transport.WebSocketTransport`
with :class:`~provide.uterm.emulator.TerminalEmulator` to provide a ready-to-use
:class:`~provide.uterm.io.Session`-compliant object.

Most of the behavior is inherited from
:class:`~provide.uterm.transport_session.TransportSession`; this module only
supplies the WebSocket transport, the UTF-8 send encoding, and the
WebSocket-specific connect argument (the ``url``).

Example::

    session = await connect_ws("wss://example.com/ws", cols=80, rows=25)
    snap = session.snapshot()
    print(snap["screen"])
    await session.send("Hello\\r")
    await session.close()
"""

from __future__ import annotations

from typing import Any

from provide.uterm.transport_session import TransportSession
from provide.uterm.transports.ws_transport import WebSocketTransport

from provide.uterm.defaults import TerminalDefaults


async def connect_ws(
    url: str,
    *,
    cols: int = 80,
    rows: int = 25,
    ping_interval: int = TerminalDefaults.WS_PING_INTERVAL,
    ping_timeout: int = TerminalDefaults.WS_PING_TIMEOUT,
    close_timeout: int = TerminalDefaults.WS_CLOSE_TIMEOUT,
    origin: str | None = None,
    additional_headers: dict[str, str] | None = None,
    control_frames: bool = False,
) -> WebSocketSession:
    """Connect to a WebSocket server and return a Session-protocol-compliant object.

    Args:
        url: Full WebSocket URL (ws:// or wss://).
        cols: Terminal width (default 80).
        rows: Terminal height (default 25).
        control_frames: When ``True``, inline DLE/STX control frames are
            parsed out of the stream and routed to
            ``session.add_control_frame_watch(...)`` instead of appearing as
            literal text on screen. Off by default — every byte goes straight
            to the emulator unmodified, matching a plain WS terminal client.

    Returns:
        A :class:`WebSocketSession` that satisfies :class:`~provide.uterm.io.Session`.

    Tip:
        To tap raw bytes from the terminal stream, call
        ``session.add_watch(...)`` on the returned session; do not monkey-patch
        the emulator internals.
    """
    session = WebSocketSession(
        url,
        cols=cols,
        rows=rows,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        close_timeout=close_timeout,
        origin=origin,
        additional_headers=additional_headers,
        control_frames=control_frames,
    )
    await session.connect()
    return session


class WebSocketSession(TransportSession):
    """WebSocket transport with pyte terminal emulation.

    Satisfies the :class:`~provide.uterm.io.Session` protocol:
    ``snapshot()``, ``send()``, ``wait_for_update()``.
    """

    def __init__(
        self,
        url: str,
        *,
        cols: int = 80,
        rows: int = 25,
        ping_interval: int = TerminalDefaults.WS_PING_INTERVAL,
        ping_timeout: int = TerminalDefaults.WS_PING_TIMEOUT,
        close_timeout: int = TerminalDefaults.WS_CLOSE_TIMEOUT,
        origin: str | None = None,
        additional_headers: dict[str, str] | None = None,
        control_frames: bool = False,
    ) -> None:
        self.url = url
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._close_timeout = close_timeout
        self._origin = origin
        self._additional_headers = additional_headers
        super().__init__(
            WebSocketTransport(), cols=cols, rows=rows, send_encoding="utf-8", control_frames=control_frames
        )

    async def _connect_transport(self) -> None:
        """Open the WebSocket connection to :attr:`url`."""
        # origin/additional_headers are omitted when None so a worker that gates
        # cross-origin WS upgrades (the 4403 path) gets the bot's allowed Origin.
        extra: dict[str, Any] = {}
        if self._origin is not None:
            extra["origin"] = self._origin
        if self._additional_headers is not None:
            extra["additional_headers"] = self._additional_headers
        await self._transport.connect(
            host="",
            port=0,
            url=self.url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            close_timeout=self._close_timeout,
            **extra,
        )
