#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI integration and network proxy classes for provide-terminal.

Two complementary proxy directions:

* **WS → telnet** (:class:`WsTerminalProxy`) — browser connects via WebSocket,
  proxy connects outbound to a raw telnet server.  Used to expose a legacy BBS
  to web browsers without touching the game server.

* **telnet → WS** (:class:`TelnetWsGateway`) — traditional telnet client connects
  via raw TCP, proxy connects outbound to a WebSocket terminal server.  Used to
  let classic telnet/SSH clients reach a WS-only game endpoint.

Requires the ``[websocket]`` extra (fastapi) for :class:`WsTerminalProxy`::

    pip install 'provide-terminal[websocket]'

Requires ``websockets`` for :class:`TelnetWsGateway` (included in ``[cli]``)::

    pip install 'provide-terminal[cli]'

Usage — mount built-in browser UI::

    from provide.terminal.fastapi import mount_terminal_ui

    mount_terminal_ui(app)  # serves terminal.html + terminal.js at /terminal

Usage — in-process session handler (WS server)::

    from provide.terminal.fastapi import create_ws_terminal_router

    async def my_handler(reader, writer, ws):
        await my_game_loop(reader, writer)

    app.include_router(create_ws_terminal_router(my_handler))

Usage — browser WS → remote telnet proxy::

    from provide.terminal.fastapi import WsTerminalProxy

    proxy = WsTerminalProxy("bbs.example.com", 23)
    app.include_router(proxy.create_router("/ws/terminal"))

Usage — telnet client → remote WS gateway::

    from provide.terminal.fastapi import TelnetWsGateway

    gw = TelnetWsGateway("wss://warp.provide.io/ws/terminal")
    server = await gw.start(host="0.0.0.0", port=2112)
    await server.serve_forever()
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

try:
    from fastapi import APIRouter, WebSocket, WebSocketDisconnect
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for WebSocket support: pip install 'provide-terminal[websocket]'") from _e

from provide.terminal.defaults import TerminalDefaults
from provide.terminal.transports.websocket import WebSocketStreamReader, WebSocketStreamWriter

if TYPE_CHECKING:
    from fastapi import FastAPI

    from provide.terminal.protocols import TerminalReader, TerminalWriter
    from provide.terminal.transports.base import ConnectionTransport

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

#: Async callback signature for :func:`create_ws_terminal_router`.
#: Receives ``(reader, writer, ws)`` — the stream shims and the raw WebSocket.
SessionHandler = Callable[
    ["TerminalReader", "TerminalWriter", WebSocket],
    Awaitable[None],
]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_ws_terminal_router(
    session_handler: SessionHandler,
    *,
    path: str = TerminalDefaults.PROXY_WS_PATH,
) -> APIRouter:
    """Create a FastAPI router with a generic WebSocket terminal endpoint.

    For each browser WebSocket connection the router:

    1. Accepts the WS handshake.
    2. Wraps the socket in :class:`~provide.terminal.transports.websocket.WebSocketStreamReader`
       and :class:`~provide.terminal.transports.websocket.WebSocketStreamWriter`.
    3. Calls *session_handler(reader, writer, ws)* and awaits it.
    4. Closes the writer and WebSocket when the handler returns.

    :class:`fastapi.WebSocketDisconnect` is caught and suppressed so that
    a normal browser close does not surface as an unhandled exception.

    Args:
        session_handler: Async callable invoked for each connection.
        path: WebSocket URL path. Defaults to ``"/ws/terminal"``.

    Returns:
        A :class:`fastapi.APIRouter` to include in your application.
    """
    router = APIRouter()

    @router.websocket(path)
    async def _ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        reader = WebSocketStreamReader(ws)
        writer = WebSocketStreamWriter(ws)
        try:
            await session_handler(reader, writer, ws)
        except WebSocketDisconnect:  # pragma: no cover
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await ws.close()

    return router


# ---------------------------------------------------------------------------
# Bidirectional proxy
# ---------------------------------------------------------------------------


class WsTerminalProxy:
    """Bidirectional WebSocket-to-transport proxy.

    Accepts browser WebSocket connections and proxies all I/O to a remote
    server via any :class:`~provide.terminal.transports.base.ConnectionTransport`
    (telnet, SSH, chaos, etc.).

    Each WebSocket connection gets its own independent transport instance and
    connection.  Both directions are pumped concurrently; when either direction
    closes (browser disconnect or remote hangup) the other is cancelled and the
    transport is disconnected.

    Args:
        host: Remote host to connect to.
        port: Remote port.
        transport_factory: Zero-argument callable that returns a fresh
            :class:`~provide.terminal.transports.base.ConnectionTransport`.
            Defaults to :class:`~provide.terminal.transports.telnet.TelnetTransport`.

    Example::

        from provide.terminal.fastapi import WsTerminalProxy

        proxy = WsTerminalProxy("bbs.example.com", 23)
        app.include_router(proxy.create_router("/ws/terminal"))
    """

    #: Milliseconds between remote-receive polls.
    _POLL_MS: int = 50

    def __init__(
        self,
        host: str,
        port: int,
        *,
        transport_factory: Callable[[], ConnectionTransport] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._transport_factory = transport_factory

    def create_router(self, path: str = TerminalDefaults.PROXY_WS_PATH) -> APIRouter:
        """Return a :class:`fastapi.APIRouter` that proxies WS connections.

        Args:
            path: WebSocket URL path. Defaults to ``"/ws/terminal"``.
        """
        return create_ws_terminal_router(self._handle, path=path)

    async def _handle(
        self,
        reader: TerminalReader,
        writer: TerminalWriter,
        _ws: WebSocket,
    ) -> None:
        from provide.terminal.transports.telnet import TelnetTransport

        factory: Callable[[], ConnectionTransport] = self._transport_factory or cast(
            "Callable[[], ConnectionTransport]", TelnetTransport
        )
        transport = factory()
        await transport.connect(self._host, self._port)
        t_b2r = asyncio.create_task(self._browser_to_remote(reader, transport))
        t_r2b = asyncio.create_task(self._remote_to_browser(transport, writer))
        try:
            _done, pending = await asyncio.wait(
                [t_b2r, t_r2b],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await transport.disconnect()

    @staticmethod
    async def _browser_to_remote(
        reader: TerminalReader,
        transport: ConnectionTransport,
    ) -> None:
        """Read from browser WebSocket and forward to remote transport."""
        while transport.is_connected():
            # WebSocketStreamReader.read(n) blocks until it has collected *n*
            # bytes or the socket closes. Terminal input is latency-sensitive,
            # so read one byte at a time here instead of buffering for 256.
            data = await reader.read(1)
            if not data:
                break
            await transport.send(data)

    @classmethod
    async def _remote_to_browser(
        cls,
        transport: ConnectionTransport,
        writer: TerminalWriter,
    ) -> None:
        """Read from remote transport and forward to browser WebSocket."""
        try:
            while transport.is_connected():
                data = await transport.receive(4096, cls._POLL_MS)
                if data:
                    writer.write(data)
                    await writer.drain()
        except ConnectionError:
            pass  # remote closed cleanly


# ---------------------------------------------------------------------------
# Built-in browser UI
# ---------------------------------------------------------------------------


def mount_terminal_ui(
    app: FastAPI,
    *,
    path: str = "/terminal",
) -> None:
    """Mount the built-in browser terminal UI at *path*.

    Serves ``terminal.html`` and ``terminal.js`` as static files.
    The page auto-connects to the WebSocket endpoint based on ``location.host``.

    Args:
        app: The FastAPI application to mount on.
        path: URL prefix. Defaults to ``"/terminal"``.
    """
    from starlette.staticfiles import StaticFiles

    frontend_path = Path(__file__).parent / "frontend"
    if not frontend_path.is_dir():
        raise RuntimeError(
            f"terminal UI assets not found at {frontend_path} — "
            "is the package installed correctly? (try: pip install 'provide-terminal[websocket]')"
        )
    app.mount(path, StaticFiles(directory=frontend_path, html=True), name="terminal-ui")
