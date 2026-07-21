#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""RFB client→server input filter (Go vnc.filterRFBInput parity).

Used for human VNC relay: pass-through handshake + non-input messages;
gate KeyEvent / PointerEvent / ClientCutText on a CanInject callback.
Nil / missing inject fn fails closed (drops inject messages).
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import BinaryIO

# Client message types (RFB 3.8)
_SET_PIXEL_FORMAT = 0
_SET_ENCODINGS = 2
_FRAMEBUFFER_UPDATE_REQUEST = 3
_KEY_EVENT = 4
_POINTER_EVENT = 5
_CLIENT_CUT_TEXT = 6

MAX_CUT_TEXT = 1 << 20  # 1 MiB

CanInjectFn = Callable[[str, str, str, str], bool]
"""(session_id, lease_id, principal_id, principal_role) -> allowed."""


def filter_rfb_client_input(
    dst: BinaryIO,
    src: BinaryIO,
    *,
    can_inject: CanInjectFn | None,
    session_id: str,
    lease_id: str,
    principal_id: str,
    principal_role: str,
) -> None:
    """Copy RFB client messages from *src* to *dst*, gating inject types.

    Raises ``ValueError`` on unsupported security type or unknown message type.
    Raises ``EOFError`` on short read.
    """
    # 1. ProtocolVersion (12 bytes)
    _copy_exact(dst, src, 12)

    # 2. Security type (1 byte) — only None (1)
    sec = _read_exact(src, 1)
    if sec[0] != 1:
        raise ValueError(f"unsupported security type {sec[0]}")
    dst.write(sec)

    # 3. ClientInit (1 byte)
    _copy_exact(dst, src, 1)

    while True:
        try:
            msg_type = _read_exact(src, 1)
        except EOFError:
            return
        t = msg_type[0]
        if t == _SET_PIXEL_FORMAT:
            dst.write(msg_type)
            _copy_exact(dst, src, 19)
        elif t == _SET_ENCODINGS:
            header = _read_exact(src, 3)
            num = struct.unpack("!H", header[1:3])[0]
            dst.write(msg_type)
            dst.write(header)
            if num > 0:
                _copy_exact(dst, src, num * 4)
        elif t == _FRAMEBUFFER_UPDATE_REQUEST:
            dst.write(msg_type)
            _copy_exact(dst, src, 9)
        elif t == _KEY_EVENT:
            payload = _read_exact(src, 7)
            if _allowed(can_inject, session_id, lease_id, principal_id, principal_role):
                dst.write(msg_type)
                dst.write(payload)
        elif t == _POINTER_EVENT:
            payload = _read_exact(src, 5)
            if _allowed(can_inject, session_id, lease_id, principal_id, principal_role):
                dst.write(msg_type)
                dst.write(payload)
        elif t == _CLIENT_CUT_TEXT:
            header = _read_exact(src, 7)
            length = struct.unpack("!I", header[3:7])[0]
            # RFB extended clipboard sets the high bit of the length field; the
            # remaining 31 bits are the real payload size (noVNC uses this).
            payload_len = length & 0x7FFFFFFF
            if payload_len > MAX_CUT_TEXT:
                # Oversized / hostile cut-text: drain what we can and drop the
                # message so the display session stays up (raising would tear
                # down the human relay and black the framebuffer).
                remaining = payload_len
                while remaining > 0:
                    chunk = src.read(min(remaining, 65_536))
                    if not chunk:
                        return
                    remaining -= len(chunk)
                continue
            payload = _read_exact(src, payload_len) if payload_len else b""
            if _allowed(can_inject, session_id, lease_id, principal_id, principal_role):
                dst.write(msg_type)
                dst.write(header)
                if payload:
                    dst.write(payload)
        else:
            raise ValueError(f"unknown RFB client message type: {t}")


def _allowed(
    can_inject: CanInjectFn | None,
    session_id: str,
    lease_id: str,
    principal_id: str,
    principal_role: str,
) -> bool:
    if can_inject is None:
        return False
    return bool(can_inject(session_id, lease_id, principal_id, principal_role))


def _read_exact(src: BinaryIO, n: int) -> bytes:
    buf = src.read(n)
    if buf is None or len(buf) < n:
        raise EOFError(f"short read: want {n}, got {0 if buf is None else len(buf)}")
    return buf


def _copy_exact(dst: BinaryIO, src: BinaryIO, n: int) -> None:
    dst.write(_read_exact(src, n))
