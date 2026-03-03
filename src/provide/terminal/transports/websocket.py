#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket-to-StreamReader/StreamWriter adapters.

Provides shim classes that make a FastAPI :class:`WebSocket` behave like
an ``asyncio.StreamReader`` / ``asyncio.StreamWriter`` pair, allowing
session handlers to run unmodified over a WebSocket connection.
"""

from __future__ import annotations

try:
    from fastapi import WebSocket, WebSocketDisconnect
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for WebSocket transport: pip install 'provide-terminal[websocket]'") from _e


class WebSocketStreamReader:
    """Adapts a FastAPI WebSocket to the ``asyncio.StreamReader`` interface.

    Only the ``read(n)`` method is implemented — that is the sole reader
    method session handlers call.  Incoming WebSocket text messages are
    buffered and served byte-by-byte as ``read(1)`` requests arrive.
    """

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._buffer = bytearray()
        self._closed = False

    async def read(self, n: int) -> bytes:
        """Return up to *n* bytes, fetching new WS messages as needed.

        Returns ``b""`` when the WebSocket disconnects.
        """
        if self._closed:
            return b""

        while len(self._buffer) < n:
            try:
                text = await self._ws.receive_text()
                self._buffer.extend(text.encode("utf-8"))
            except (WebSocketDisconnect, RuntimeError):
                self._closed = True
                return b""

        result = bytes(self._buffer[:n])
        del self._buffer[:n]
        return result


class WebSocketStreamWriter:
    """Adapts a FastAPI WebSocket to the ``asyncio.StreamWriter`` interface.

    Implements ``write()``, ``drain()``, ``get_extra_info()``, ``close()``,
    and ``wait_closed()`` — the methods session handlers use.

    Calls to ``write()`` buffer data; ``drain()`` flushes the buffer as a
    single WebSocket text message.
    """

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._pending = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        """Append *data* to the pending output buffer."""
        if not self._closed:
            self._pending.extend(data)

    async def drain(self) -> None:
        """Flush the pending buffer as a WebSocket text message."""
        if self._pending and not self._closed:
            text = bytes(self._pending).decode("utf-8", errors="replace")
            self._pending.clear()
            try:
                await self._ws.send_text(text)
            except (WebSocketDisconnect, RuntimeError):
                self._closed = True

    def get_extra_info(self, key: str, default: object = None) -> object:
        """Return connection metadata.

        Handles ``"peername"`` to return ``(host, port)`` tuple.
        """
        match key:
            case "peername" if client := self._ws.client:
                return (client.host, client.port)
            case _:
                return default

    def close(self) -> None:
        """Mark the writer as closed."""
        self._closed = True

    async def wait_closed(self) -> None:
        """No-op — WebSocket lifecycle is handled by the endpoint."""
