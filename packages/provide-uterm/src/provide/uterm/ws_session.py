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

from provide.uterm.transport_session import TransportSession
from provide.uterm.transports.ws_transport import WebSocketTransport


async def connect_ws(
    url: str,
    *,
    cols: int = 80,
    rows: int = 25,
) -> WebSocketSession:
    """Connect to a WebSocket server and return a Session-protocol-compliant object.

    Args:
        url: Full WebSocket URL (ws:// or wss://).
        cols: Terminal width (default 80).
        rows: Terminal height (default 25).

    Returns:
        A :class:`WebSocketSession` that satisfies :class:`~provide.uterm.io.Session`.

    Tip:
        To tap raw bytes from the terminal stream, call
        ``session.add_watch(...)`` on the returned session; do not monkey-patch
        the emulator internals.
    """
    session = WebSocketSession(url, cols=cols, rows=rows)
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
    ) -> None:
        self.url = url
        super().__init__(WebSocketTransport(), cols=cols, rows=rows, send_encoding="utf-8")

    async def _connect_transport(self) -> None:
        """Open the WebSocket connection to :attr:`url`."""
        await self._transport.connect(host="", port=0, url=self.url)
