#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cloudflare Workers WebSocket-to-StreamReader/StreamWriter adapters.

Provides adapters that make a CF Workers (Durable Object) WebSocket behave
like ``asyncio.StreamReader``/``StreamWriter``, allowing session handlers
to run unmodified over a CF Workers WebSocket.

Key difference from the FastAPI :mod:`provide.uterm.transports.websocket`
adapters: CF Workers WebSockets are *event-based* — messages arrive via the
``webSocketMessage`` handler — rather than *pull-based*.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class CFWebSocketStreamReader:
    """Adapts a CF Workers WebSocket to ``asyncio.StreamReader`` interface.

    CF Workers WebSockets are event-based — messages arrive via the
    ``webSocketMessage`` handler.  This reader uses a deque to buffer
    incoming messages and serves them byte-by-byte as ``read(n)`` requests
    arrive.

    Parameters:
        encoding: Character encoding for incoming text messages.
            Defaults to ``"latin-1"`` for CP437 character preservation.
    """

    def __init__(self, *, encoding: str = "latin-1") -> None:
        self._buffer = bytearray()
        self._message_queue: deque[str] = deque()
        self._closed = False
        self._waiting: asyncio.Future[Any] | None = None
        self._encoding = encoding

    def queue_message(self, text: str) -> None:
        """Called by ``webSocketMessage`` handler to queue incoming text."""
        if not self._closed and text:
            self._message_queue.append(text)
            if self._waiting and not self._waiting.done():
                self._waiting.set_result(None)

    def close(self) -> None:
        """Mark reader as closed."""
        self._closed = True
        if self._waiting and not self._waiting.done():
            self._waiting.set_result(None)

    async def read(self, n: int) -> bytes:
        """Return up to *n* bytes, pulling from queue as needed.

        Returns ``b""`` when closed, which causes ``Session.read_char()``
        to set ``session.active = False``.
        """
        if self._closed:
            return b""

        while len(self._buffer) < n:
            if self._message_queue:
                text = self._message_queue.popleft()
                self._buffer.extend(text.encode(self._encoding, errors="replace"))
            else:
                if self._closed:
                    return b""
                # No timeout — the DO event loop goes idle and the Durable
                # Object can hibernate until the next webSocketMessage.
                self._waiting = asyncio.Future()
                try:
                    await self._waiting
                finally:
                    self._waiting = None
                if self._closed:
                    return b""

        result = bytes(self._buffer[:n])
        del self._buffer[:n]
        return result


class CFWebSocketStreamWriter:
    """Adapts a CF Workers WebSocket to ``asyncio.StreamWriter`` interface.

    Implements ``write()``, ``drain()``, ``get_extra_info()``, ``close()``,
    and ``wait_closed()`` — the methods session handlers use.

    Calls to ``write()`` buffer data; ``drain()`` flushes the buffer as a
    single WebSocket text message.

    Parameters:
        ws: CF Workers WebSocket object (JS proxy).
        peername: ``(host, port)`` tuple for connection metadata.
    """

    def __init__(self, ws: Any, peername: tuple[str, int] = ("unknown", 0)) -> None:
        self._ws = ws
        self._pending = bytearray()
        self._closed = False
        self._peername = peername
        self._batching = False

    def write(self, data: bytes) -> None:
        """Append *data* to the pending output buffer."""
        if not self._closed:
            self._pending.extend(data)

    def begin_batch(self) -> None:
        """Start batching mode — ``drain()`` accumulates without sending.

        Call :meth:`flush_batch` to send all accumulated data in one
        ``ws.send()`` call.  Used to coalesce the welcome banner sequence
        (~15 sends) into a single Pyodide→JS bridge crossing.
        """
        self._batching = True

    async def flush_batch(self) -> None:
        """End batching mode and flush all pending data in a single ``ws.send()``."""
        self._batching = False
        await self.drain()

    async def drain(self) -> None:
        """Flush pending buffer as a WebSocket text message."""
        if self._batching or not self._pending or self._closed:
            return
        # Decode with latin-1 (maps bytes 0x00-0xFF → U+0000-U+00FF losslessly)
        # so raw terminal bytes — including non-UTF-8 CP437 line-drawing chars
        # and 8-bit binary — round-trip exactly. The matching reader decodes
        # incoming text with latin-1 for the same CP437-preservation reason; a
        # utf-8/errors="replace" writer would corrupt high bytes to U+FFFD
        # before transmission, so the reader could never recover them. latin-1
        # cannot fail, so no errors= handler is needed.
        text = bytes(self._pending).decode("latin-1")
        self._pending.clear()
        try:
            self._ws.send(text)
        except Exception:
            self._closed = True

    def get_extra_info(self, key: str, default: object = None) -> object:
        """Return connection metadata (``"peername"`` → ``(host, port)``)."""
        if key == "peername":
            return self._peername
        return default

    def close(self) -> None:
        """Mark writer as closed."""
        self._closed = True

    async def wait_closed(self) -> None:
        """No-op — WebSocket lifecycle is handled by the DO."""
