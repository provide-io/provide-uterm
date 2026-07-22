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

import contextlib
import struct
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, BinaryIO

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
    dst_lock: AbstractContextManager[Any] | None = None,
    on_client_ready: Callable[[], None] | None = None,
) -> None:
    """Copy RFB client messages from *src* to *dst*, gating inject types.

    Each RFB message is written to *dst* in a single ``write`` guarded by
    *dst_lock* (a no-op context when ``None``). This keeps writes atomic so a
    concurrent writer of *dst* — e.g. the update-driver thread in
    :func:`run_human_relay_streams` that injects periodic
    ``FramebufferUpdateRequest`` frames — never interleaves mid-message.

    *on_client_ready*, if given, is invoked once the client's FIRST
    ``FramebufferUpdateRequest`` has been forwarded. By then the client's
    ``SetPixelFormat`` + ``SetEncodings`` (which precede that request) are already
    upstream, so a driver that starts injecting requests won't race ahead of the
    client's pixel format — otherwise the server may answer the driver's request
    in its native format and the client renders those frames with swapped colours.

    Raises ``ValueError`` on unsupported security type or unknown message type.
    Raises ``EOFError`` on short read.
    """
    guard: AbstractContextManager[Any] = dst_lock if dst_lock is not None else contextlib.nullcontext()

    def emit(data: bytes) -> None:
        with guard:
            dst.write(data)

    # 1. ProtocolVersion (12 bytes)
    emit(_read_exact(src, 12))

    # 2. Security type (1 byte) — only None (1)
    sec = _read_exact(src, 1)
    if sec[0] != 1:
        raise ValueError(f"unsupported security type {sec[0]}")
    emit(sec)

    # 3. ClientInit (1 byte)
    emit(_read_exact(src, 1))

    client_ready_fired = False
    while True:
        try:
            msg_type = _read_exact(src, 1)
        except EOFError:
            return
        t = msg_type[0]
        if t == _SET_PIXEL_FORMAT:
            emit(msg_type + _read_exact(src, 19))
        elif t == _SET_ENCODINGS:
            header = _read_exact(src, 3)
            num = struct.unpack("!H", header[1:3])[0]
            body = _read_exact(src, num * 4) if num > 0 else b""
            emit(msg_type + header + body)
        elif t == _FRAMEBUFFER_UPDATE_REQUEST:
            emit(msg_type + _read_exact(src, 9))
            if on_client_ready is not None and not client_ready_fired:
                client_ready_fired = True
                on_client_ready()
        elif t == _KEY_EVENT:
            payload = _read_exact(src, 7)
            if _allowed(can_inject, session_id, lease_id, principal_id, principal_role):
                emit(msg_type + payload)
        elif t == _POINTER_EVENT:
            payload = _read_exact(src, 5)
            if _allowed(can_inject, session_id, lease_id, principal_id, principal_role):
                emit(msg_type + payload)
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
                emit(msg_type + header + payload)
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
