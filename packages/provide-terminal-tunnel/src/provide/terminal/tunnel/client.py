#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Async WebSocket tunnel client with reconnect support."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, cast

from websockets.asyncio.client import connect

from provide.terminal.tunnel.protocol import (
    CHANNEL_DATA,
    FLAG_EOF,
    TunnelFrame,
    decode_frame,
    encode_control,
    encode_frame,
)

log = logging.getLogger(__name__)

BACKOFF_SCHEDULE: tuple[int, ...] = (1, 2, 5, 10, 30)


class _WebSocketState(Protocol):
    name: str


class _WebSocketProtocol(Protocol):
    state: _WebSocketState


class _TunnelWebSocket(Protocol):
    protocol: _WebSocketProtocol

    async def close(self) -> object: ...

    async def send(self, message: bytes) -> object: ...

    async def recv(self) -> bytes | str: ...


class TunnelClient:
    """Async WebSocket tunnel client.

    Connects to a CF tunnel endpoint and sends/receives binary tunnel frames.
    """

    def __init__(self, ws_url: str, token: str) -> None:
        self._ws_url = ws_url
        self._token = token
        self._ws: _TunnelWebSocket | None = None

    @property
    def connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return self._ws.protocol.state.name == "OPEN"
        except Exception:
            return False

    async def connect(self) -> None:
        """Establish the WebSocket connection."""
        self._ws = cast(
            "_TunnelWebSocket",
            await connect(
                self._ws_url,
                additional_headers={"Authorization": f"Bearer {self._token}"},
            ),
        )

    async def close(self) -> None:
        """Close the WebSocket connection (idempotent)."""
        ws = self._ws
        self._ws = None
        if ws is not None:
            await ws.close()

    async def open_terminal(self, cols: int, rows: int) -> None:
        """Send a control message to open a terminal channel."""
        msg = {
            "type": "open",
            "channel": 1,
            "tunnel_type": "terminal",
            "term_size": [cols, rows],
        }
        await self._send_raw(encode_control(msg))

    async def send_data(self, data: bytes, channel: int = CHANNEL_DATA) -> None:
        """Send a data frame on the given channel."""
        self._require_connected()
        ws = self._ws
        if ws is None:  # pragma: no cover — guarded by _require_connected
            msg = "not connected"
            raise RuntimeError(msg)
        await ws.send(encode_frame(channel, data))

    async def send_resize(self, cols: int, rows: int) -> None:
        """Send a resize control message."""
        msg = {"type": "resize", "channel": 1, "cols": cols, "rows": rows}
        await self._send_raw(encode_control(msg))

    async def send_eof(self, channel: int = CHANNEL_DATA) -> None:
        """Send an EOF frame on the given channel."""
        self._require_connected()
        ws = self._ws
        if ws is None:  # pragma: no cover — guarded by _require_connected
            msg = "not connected"
            raise RuntimeError(msg)
        await ws.send(encode_frame(channel, b"", flags=FLAG_EOF))

    async def recv(self) -> TunnelFrame:
        """Receive and decode a tunnel frame."""
        self._require_connected()
        ws = self._ws
        if ws is None:  # pragma: no cover — guarded by _require_connected
            msg = "not connected"
            raise RuntimeError(msg)
        raw = await ws.recv()
        if isinstance(raw, str):
            raw = raw.encode("latin-1")
        return decode_frame(raw)

    async def reconnect_loop(self, max_attempts: int = 0) -> None:
        """Attempt to connect with exponential backoff.

        Args:
            max_attempts: Maximum attempts (0 = unlimited).
        """
        for attempt in range(max_attempts if max_attempts else 2**31):
            delay = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
            await asyncio.sleep(delay)
            try:
                await self.connect()
                return
            except Exception:
                log.warning("reconnect attempt %d failed, retrying in %ds", attempt + 1, delay)

    async def _send_raw(self, data: bytes) -> None:
        self._require_connected()
        ws = self._ws
        if ws is None:  # pragma: no cover — guarded by _require_connected
            msg = "not connected"
            raise RuntimeError(msg)
        await ws.send(data)

    def _require_connected(self) -> None:
        if self._ws is None:
            msg = "not connected"
            raise RuntimeError(msg)
