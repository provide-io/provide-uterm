#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Binary tunnel frame protocol: encode/decode wire frames."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

CHANNEL_CONTROL: int = 0x00
CHANNEL_DATA: int = 0x01
CHANNEL_TCP: int = 0x02
CHANNEL_HTTP: int = 0x03

FLAG_DATA: int = 0x00
FLAG_EOF: int = 0x01


class TunnelProtocolError(ValueError):
    """Raised on malformed tunnel frames or invalid arguments."""


@dataclass(frozen=True)
class TunnelFrame:
    """A decoded tunnel frame."""

    channel: int
    flags: int
    payload: bytes

    @property
    def is_eof(self) -> bool:
        return (self.flags & FLAG_EOF) != 0

    @property
    def is_control(self) -> bool:
        return self.channel == CHANNEL_CONTROL


def encode_frame(channel: int, payload: bytes, *, flags: int = FLAG_DATA) -> bytes:
    """Encode a tunnel frame: [channel][flags][payload]."""
    if not (0 <= channel <= 0xFF):
        msg = "channel must be 0..255"
        raise TunnelProtocolError(msg)
    if not (0 <= flags <= 0xFF):
        msg = "flags must be 0..255"
        raise TunnelProtocolError(msg)
    return bytes([channel, flags]) + payload


def decode_frame(data: bytes) -> TunnelFrame:
    """Decode a tunnel frame from raw bytes."""
    if len(data) < 2:
        msg = "frame too short"
        raise TunnelProtocolError(msg)
    return TunnelFrame(channel=data[0], flags=data[1], payload=data[2:])


def encode_control(msg: dict[str, Any]) -> bytes:
    """Encode a control message as a frame on channel 0 (compact JSON)."""
    if "type" not in msg:
        err = "control message must have a 'type' key"
        raise TunnelProtocolError(err)
    payload = json.dumps(msg, separators=(",", ":")).encode()
    return encode_frame(CHANNEL_CONTROL, payload)


def decode_control(payload: bytes) -> dict[str, Any]:
    """Decode a control payload (JSON bytes) into a dict."""
    try:
        obj = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = "invalid control payload"
        raise TunnelProtocolError(msg) from exc
    if not isinstance(obj, dict):
        msg = "control payload must be a JSON object"
        raise TunnelProtocolError(msg)
    return obj
