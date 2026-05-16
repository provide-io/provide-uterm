#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tunnel binary frame handler for the CF Durable Object.

Bridges binary tunnel frames to the existing SessionRuntime browser
multiplexing, hijack, and recording infrastructure.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.uterm.cloudflare.contracts import RuntimeProtocol

logger = logging.getLogger(__name__)

_CHANNEL_CONTROL = 0x00
_CHANNEL_DATA = 0x01
_CHANNEL_HTTP = 0x03
_FLAG_EOF = 0x01
_MIN_FRAME = 2


def is_tunnel_message(message: Any) -> bool:
    """Return True if the message is a binary tunnel frame."""
    return isinstance(message, (bytes, bytearray, memoryview))


def decode_tunnel_frame(data: bytes) -> tuple[int, int, bytes]:
    """Decode a binary tunnel frame -> (channel, flags, payload)."""
    if len(data) < _MIN_FRAME:
        return (-1, 0, b"")
    return (data[0], data[1], data[_MIN_FRAME:])


def encode_tunnel_input(data: str, channel: int = _CHANNEL_DATA) -> bytes:
    """Encode browser input as a tunnel frame to send to the agent."""
    return bytes([channel, 0x00]) + data.encode("utf-8")


def encode_tunnel_control(msg: dict[str, Any]) -> bytes:
    """Encode a control message as a tunnel frame for the agent."""
    payload = json.dumps(msg, separators=(",", ":")).encode()
    return bytes([_CHANNEL_CONTROL, 0x00]) + payload


async def handle_tunnel_message(
    runtime: RuntimeProtocol,
    ws: object,
    message: bytes,
) -> None:
    """Process a binary tunnel frame from the tunnel agent."""
    channel, flags, payload = decode_tunnel_frame(message)

    if channel == _CHANNEL_CONTROL:
        await _handle_control(runtime, ws, payload)
        return

    if flags & _FLAG_EOF:
        logger.info("tunnel EOF on channel %d for worker_id=%s", channel, runtime.worker_id)
        return

    if channel == _CHANNEL_HTTP and payload:
        try:
            http_msg = json.loads(payload)
            http_msg["_channel"] = "http"
            await runtime.broadcast_worker_frame(http_msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("tunnel_bad_http_frame worker_id=%s", runtime.worker_id)
        return

    if channel >= _CHANNEL_DATA and payload:
        text = payload.decode("utf-8", errors="replace")
        frame = {"type": "term", "data": text, "ts": time.time()}
        await runtime.broadcast_worker_frame(frame)
        if runtime.last_snapshot is None:
            runtime.last_snapshot = {"type": "snapshot", "screen": text, "ts": time.time()}
        else:
            screen = str(runtime.last_snapshot.get("screen", "")) + text
            if len(screen) > 32768:
                screen = screen[-32768:]
            runtime.last_snapshot = {"type": "snapshot", "screen": screen, "ts": time.time()}


async def _handle_control(runtime: RuntimeProtocol, _ws: object, payload: bytes) -> None:
    """Handle a control channel message from the tunnel agent."""
    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("invalid tunnel control payload from worker_id=%s", runtime.worker_id)
        return

    msg_type = msg.get("type")
    if msg_type == "open":
        runtime.lifecycle_state = "running"
        logger.info(
            "tunnel open: type=%s channel=%s term_size=%s worker_id=%s",
            msg.get("tunnel_type"),
            msg.get("channel", 1),
            msg.get("term_size"),
            runtime.worker_id,
        )
    elif msg_type == "resize":
        logger.debug("tunnel resize: %dx%d worker_id=%s", msg.get("cols", 80), msg.get("rows", 24), runtime.worker_id)
    elif msg_type == "close":
        logger.info("tunnel close channel %d for worker_id=%s", msg.get("channel", 1), runtime.worker_id)
    elif msg_type == "error":
        logger.warning("tunnel error from agent: %s worker_id=%s", msg.get("message", "unknown"), runtime.worker_id)
    else:
        logger.debug("unknown tunnel control type=%s worker_id=%s", msg_type, runtime.worker_id)
