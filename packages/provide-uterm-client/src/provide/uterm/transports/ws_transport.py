#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocketTransport — WebSocket client implementing ConnectionTransport."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    from websockets.protocol import State
except ImportError as _e:
    raise ImportError(
        "websockets is required for WebSocket transport: pip install 'provide-uterm-client[websocket]'"
    ) from _e

from provide.telemetry import get_logger

from provide.uterm.transports.base import ConnectionTransport

logger = get_logger(__name__)


class WebSocketTransport(ConnectionTransport):
    """WebSocket client implementing the ConnectionTransport interface."""

    def __init__(self) -> None:
        self._url: str | None = None
        self._ws: Any = None
        self._connected = False

    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        """Connect to WebSocket.

        Args:
            host: Ignored if url is provided, otherwise used for wss://host:port.
            port: Ignored if url is provided.
            url: The full WebSocket URL to connect to.
        """
        # A `url` kwarg wins; otherwise build wss://host:port. The fallback
        # always yields a non-empty URL, so the only failure mode below is a
        # real websockets.connect error (no unreachable "no URL" branch).
        self._url = kwargs.get("url") or f"wss://{host}:{port}"

        forwarded: dict[str, Any] = {}
        for key in ("max_size", "ping_interval", "ping_timeout", "close_timeout"):
            value = kwargs.get(key)
            if value is not None:
                forwarded[key] = value

        try:
            self._ws = await websockets.connect(self._url, **forwarded)
            self._connected = True
            logger.debug("WebSocketTransport connected to %s", self._url)
        except Exception as exc:
            self._connected = False
            raise ConnectionError(f"Failed to connect to {self._url}") from exc

    async def disconnect(self) -> None:
        """Close the connection."""
        self._connected = False
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def send(self, data: bytes) -> None:
        """Send bytes as a WebSocket TEXT frame.

        The ``websockets`` library maps ``str`` -> TEXT (opcode 1) and ``bytes``
        -> BINARY (opcode 2). The uterm control plane and the text-based
        Cloudflare Worker speak TEXT frames, so the incoming UTF-8 bytes (already
        encoded by ``TransportSession.send`` with ``errors="replace"``) are
        decoded back to ``str`` here to force a TEXT frame — a BINARY frame
        arrives at the worker as a ``JsProxy`` and is silently dropped.
        ``errors="replace"`` keeps the decode total for any direct-bytes caller.
        Telnet stays BINARY via its own (byte-protocol) transport.
        """
        if not self._connected or not self._ws:
            raise ConnectionError("Not connected")

        try:
            await self._ws.send(data.decode("utf-8", errors="replace"))
        except ConnectionClosed as exc:
            await self.disconnect()
            raise ConnectionError("Connection closed") from exc

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        """Receive bytes.

        Note: ``max_bytes`` is advisory and ignored — WebSocket is a
        message-framed transport, so each ``recv()`` yields one whole frame.
        Chunking to ``max_bytes`` would corrupt that framing, so the full
        message is always returned.
        """
        if not self._connected or not self._ws:
            raise ConnectionError("Not connected")

        try:
            message = await asyncio.wait_for(self._ws.recv(), timeout=timeout_ms / 1000.0)
            # Text frames carry terminal bytes; latin-1 round-trips bytes
            # 0..255 1:1 (errors="replace" guards any non-encodable code point).
            if isinstance(message, str):
                return message.encode("latin-1", errors="replace")
            return bytes(message)
        except TimeoutError:
            # asyncio.TimeoutError is TimeoutError on 3.11+: a read timeout
            # yields no data rather than tearing down the connection.
            return b""
        except ConnectionClosed as exc:
            await self.disconnect()
            raise ConnectionError("Connection closed") from exc
        except Exception as exc:
            # Narrow by design: CancelledError is a BaseException on 3.11+, so
            # it is NOT swallowed here and cancellation propagates correctly.
            await self.disconnect()
            raise ConnectionError("WebSocket receive error") from exc

    def is_connected(self) -> bool:
        """Check if connection is active.

        websockets 16.0 ``ClientConnection`` has no ``.closed`` attribute;
        liveness is read from the ``.state`` enum (``State.OPEN``).
        """
        return self._connected and self._ws is not None and self._ws.state is State.OPEN
