#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cloudflare Workers WebSocket-to-StreamReader/StreamWriter adapters.

Makes a CF Workers (Durable Object) WebSocket behave like
``asyncio.StreamReader``/``StreamWriter``, so session handlers written against
the stream interfaces run unmodified on a Durable Object.

Key difference from the pull-based adapters in :mod:`provide.uterm.ws_session`:
CF Workers WebSockets are *event-based* — messages arrive via the
``webSocketMessage`` handler rather than being awaited from a socket.

This lives in the core library rather than in a Cloudflare *application*
package because it is a reusable building block with no application state and
no dependency beyond the standard library: every Python-on-Durable-Object
consumer needs exactly this adapter, and a copy inside any one deployable app
is a copy the other apps cannot import.

Encoding is a constructor parameter on both halves rather than a hard-coded
constant, because consumers differ in what they put on the wire. A session that
writes raw CP437 terminal bytes needs ``latin-1`` (the default), which maps
bytes 0x00-0xFF to U+0000-U+00FF losslessly and cannot fail. A session that
writes text already encoded as UTF-8 needs ``utf-8``, or every non-ASCII
character is decoded as individual latin-1 code points and re-encoded by the JS
side into mojibake. Both are correct; only the consumer knows which applies.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# Caps to bound buffer growth if drain/flush is never called or a consumer
# stalls. Generous for normal interactive terminal traffic (a full screen
# render is a few KB) while still failing fast on a runaway producer.
MAX_READER_BUFFER_BYTES = 1_048_576  # 1 MiB
MAX_MESSAGE_QUEUE_DEPTH = 1000
MAX_WRITER_PENDING_BYTES = 1_048_576  # 1 MiB


class CFWebSocketStreamReader:
    """Adapts a CF Workers WebSocket to the ``asyncio.StreamReader`` interface.

    CF Workers WebSockets are event-based — messages arrive via the
    ``webSocketMessage`` handler. This reader buffers incoming messages in a
    deque and serves them byte-by-byte as ``read(n)`` requests arrive.

    Parameters:
        encoding: Character encoding for incoming text messages. Defaults to
            ``"latin-1"`` for CP437 preservation; see the module docstring.
    """

    def __init__(self, *, encoding: str = "latin-1") -> None:
        self._buffer = bytearray()
        self._message_queue: deque[str] = deque()
        self._closed = False
        self._waiting: asyncio.Future[Any] | None = None
        self._encoding = encoding

    def queue_message(self, text: str) -> None:
        """Queue an incoming text message from the ``webSocketMessage`` handler."""
        if not self._closed and text:
            if len(self._message_queue) >= MAX_MESSAGE_QUEUE_DEPTH:
                # Slow or stuck reader — drop oldest to bound memory rather
                # than let an unbounded producer balloon DO memory.
                dropped = self._message_queue.popleft()
                logger.warning(
                    "cf_reader_queue_full cap=%d dropped_len=%d",
                    MAX_MESSAGE_QUEUE_DEPTH,
                    len(dropped),
                )
            self._message_queue.append(text)
            if self._waiting and not self._waiting.done():
                self._waiting.set_result(None)

    def close(self) -> None:
        """Mark the reader closed and release buffered data."""
        self._closed = True
        # Release buffered data so a closed-but-not-yet-collected reader does
        # not pin potentially large queued messages.
        self._message_queue.clear()
        self._buffer.clear()
        if self._waiting and not self._waiting.done():
            self._waiting.set_result(None)

    async def read(self, n: int) -> bytes:
        """Return up to *n* bytes, pulling from the queue as needed.

        Returns ``b""`` when closed, which callers treat as end-of-stream.
        """
        if self._closed:
            return b""

        while len(self._buffer) < n:
            if self._message_queue:
                text = self._message_queue.popleft()
                encoded = text.encode(self._encoding, errors="replace")
                if len(self._buffer) + len(encoded) > MAX_READER_BUFFER_BYTES:
                    overflow = len(self._buffer) + len(encoded) - MAX_READER_BUFFER_BYTES
                    logger.warning(
                        "cf_reader_buffer_cap cap=%d dropped_bytes=%d",
                        MAX_READER_BUFFER_BYTES,
                        overflow,
                    )
                    del self._buffer[:overflow]
                self._buffer.extend(encoded)
            else:
                if self._closed:
                    return b""

                # No timeout — the DO event loop goes idle so the Durable
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
    """Adapts a CF Workers WebSocket to the ``asyncio.StreamWriter`` interface.

    Implements ``write()``, ``drain()``, ``get_extra_info()``, ``close()`` and
    ``wait_closed()`` — the methods session handlers use. ``write()`` buffers;
    ``drain()`` flushes the buffer as a single WebSocket text message.

    Parameters:
        ws: CF Workers WebSocket object (JS proxy).
        peername: ``(host, port)`` tuple for connection metadata.
        encoding: Character encoding used to decode pending bytes back to the
            text passed to ``ws.send()``. Must match how the consumer encoded
            them; see the module docstring.
    """

    def __init__(
        self,
        ws: Any,
        peername: tuple[str, int] = ("unknown", 0),
        *,
        encoding: str = "latin-1",
    ) -> None:
        self._ws = ws
        self._pending = bytearray()
        self._closed = False
        self._peername = peername
        self._encoding = encoding
        self._batching = False  # When True, drain() defers ws.send() until flush_batch()
        # Monotonic count of ws.send() calls that actually crossed to the JS
        # side. Pairs with a client-side received-bytes counter: a consumer
        # that rendered output the client never observed is ambiguous between
        # "never sent" and "sent but lost downstream", and only a send-side
        # counter separates them.
        self.send_seq = 0

    def write(self, data: bytes) -> None:
        """Append *data* to the pending output buffer."""
        if self._closed:
            return
        # Cap pending growth — if drain()/flush_batch() is never called (a
        # stalled batch, a dead socket) writes would accumulate unbounded.
        if len(self._pending) + len(data) > MAX_WRITER_PENDING_BYTES:
            overflow = len(self._pending) + len(data) - MAX_WRITER_PENDING_BYTES
            logger.warning(
                "cf_writer_pending_cap cap=%d dropped_bytes=%d batching=%s",
                MAX_WRITER_PENDING_BYTES,
                overflow,
                self._batching,
            )
            if overflow >= len(self._pending):
                self._pending.clear()
            else:
                del self._pending[:overflow]
        self._pending.extend(data)

    def begin_batch(self) -> None:
        """Start batching mode — ``drain()`` accumulates without sending.

        Call :meth:`flush_batch` to send everything accumulated in one
        ``ws.send()``, coalescing a multi-write sequence (a welcome banner, a
        full screen repaint) into a single Pyodide→JS bridge crossing.
        """
        self._batching = True

    async def flush_batch(self) -> None:
        """End batching mode and flush all pending data in a single ``ws.send()``."""
        self._batching = False
        await self.drain()

    async def drain(self) -> None:
        """Flush the pending buffer as a WebSocket text message."""
        if self._batching or not self._pending or self._closed:
            # Returning here sends nothing, and the caller cannot tell:
            # drain() has no return value and raises nothing, so a session
            # writing to a CLOSED writer sees exactly what a successful send
            # looks like. Pending bytes at this point are output the consumer
            # believes it delivered — say so, or the only remaining evidence
            # is a client that never rendered.
            if self._pending and (self._closed or self._batching):
                logger.info(
                    "cf_writer_drain_skipped reason=%s pending_bytes=%d",
                    "closed" if self._closed else "batching",
                    len(self._pending),
                )
            return
        text = bytes(self._pending).decode(self._encoding, errors="replace")
        self._pending.clear()
        try:
            self._ws.send(text)
            self.send_seq += 1
            logger.debug("cf_writer_send send_seq=%d bytes=%d", self.send_seq, len(text))
        except Exception as e:
            logger.error(
                "cf_writer_send_failed error_type=%s text_len=%d error=%s",
                type(e).__name__,
                len(text),
                e,
            )
            self._closed = True

    def get_extra_info(self, key: str, default: object = None) -> object:
        """Return connection metadata (``"peername"`` → ``(host, port)``)."""
        if key == "peername":
            return self._peername
        return default

    def close(self) -> None:
        """Mark the writer closed and discard pending output."""
        self._closed = True
        # Unflushed bytes cannot be delivered once closed; holding them only
        # pins memory on the DO.
        self._pending.clear()

    async def wait_closed(self) -> None:
        """No-op — WebSocket lifecycle is handled by the DO."""
