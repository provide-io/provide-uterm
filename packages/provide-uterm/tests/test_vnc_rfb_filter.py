#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Go filterRFBInput parity — human VNC relay input gate."""

from __future__ import annotations

import io
import struct

import pytest
from provide.uterm.vnc.rfb_filter import filter_rfb_client_input


def _handshake() -> bytes:
    return b"RFB 003.008\n" + bytes([1]) + bytes([1])


def _key_event() -> bytes:
    # type 4 + 7 bytes
    return bytes([4]) + bytes(7)


def test_nil_can_inject_drops_key() -> None:
    src = io.BytesIO(_handshake() + _key_event())
    dst = io.BytesIO()
    filter_rfb_client_input(
        dst,
        src,
        can_inject=None,
        session_id="s",
        lease_id="l",
        principal_id="p",
        principal_role="operator",
    )
    # handshake only: 12 + 1 + 1 = 14
    assert len(dst.getvalue()) == 14


def test_operator_with_lease_forwards_key() -> None:
    src = io.BytesIO(_handshake() + _key_event())
    dst = io.BytesIO()

    def allow(_sid: str, lid: str, _pid: str, role: str) -> bool:
        return bool(lid) and role in {"operator", "admin"}

    filter_rfb_client_input(
        dst,
        src,
        can_inject=allow,
        session_id="s",
        lease_id="lease-1",
        principal_id="bob",
        principal_role="operator",
    )
    assert len(dst.getvalue()) == 14 + 8


def test_viewer_drops_key() -> None:
    src = io.BytesIO(_handshake() + _key_event())
    dst = io.BytesIO()
    filter_rfb_client_input(
        dst,
        src,
        can_inject=lambda *_a: False,
        session_id="s",
        lease_id="l",
        principal_id="v",
        principal_role="viewer",
    )
    assert len(dst.getvalue()) == 14


def test_bad_security_type() -> None:
    src = io.BytesIO(b"RFB 003.008\n" + bytes([2]))
    dst = io.BytesIO()
    with pytest.raises(ValueError, match="security type"):
        filter_rfb_client_input(
            dst,
            src,
            can_inject=None,
            session_id="s",
            lease_id="",
            principal_id="",
            principal_role="viewer",
        )


def test_cut_text_too_large_is_dropped_not_fatal() -> None:
    # handshake + type 6 + padding3 + length + oversized body + a later key
    # (proves the filter continues after dropping the cut-text).
    length = (1 << 20) + 1
    pad = b"x" * length
    key = bytes([4]) + bytes(7)
    body = _handshake() + bytes([6]) + bytes(3) + struct.pack("!I", length) + pad + key
    src = io.BytesIO(body)
    dst = io.BytesIO()
    filter_rfb_client_input(
        dst,
        src,
        can_inject=lambda *_a: True,
        session_id="s",
        lease_id="l",
        principal_id="p",
        principal_role="admin",
    )
    # Handshake only + key event (cut-text dropped).
    assert len(dst.getvalue()) == 14 + 8


def test_cut_text_extended_clipboard_high_bit() -> None:
    """noVNC extended clipboard: high bit of length is a flag, not part of size."""
    text = b"flags"  # 5-byte extended payload
    # High bit set + payload length 5.
    length_field = 0x80000000 | len(text)
    body = _handshake() + bytes([6]) + bytes(3) + struct.pack("!I", length_field) + text
    src = io.BytesIO(body)
    dst = io.BytesIO()
    filter_rfb_client_input(
        dst,
        src,
        can_inject=lambda *_a: True,
        session_id="s",
        lease_id="l",
        principal_id="p",
        principal_role="admin",
    )
    # Handshake (14) + type + header(7) + payload(5)
    assert len(dst.getvalue()) == 14 + 1 + 7 + 5


def _drive(body: bytes, *, can_inject=None) -> bytes:
    """Run the filter over *body* and return the forwarded bytes."""
    dst = io.BytesIO()
    filter_rfb_client_input(
        dst,
        io.BytesIO(body),
        can_inject=can_inject,
        session_id="s",
        lease_id="l",
        principal_id="p",
        principal_role="operator",
    )
    return dst.getvalue()


def test_set_pixel_format_passes_through() -> None:
    # type 0 + 19-byte pixel format, always forwarded (not an inject type).
    out = _drive(_handshake() + bytes([0]) + b"P" * 19)
    assert out == _handshake() + bytes([0]) + b"P" * 19


def test_set_encodings_passes_through() -> None:
    # type 2 + [pad, num_hi, num_lo] + num*4 encoding words.
    body = _handshake() + bytes([2]) + bytes([0, 0, 2]) + b"E" * 8
    assert _drive(body) == body


def test_set_encodings_zero_count() -> None:
    # num == 0: header forwarded, no encoding words to copy.
    body = _handshake() + bytes([2]) + bytes([0, 0, 0])
    assert _drive(body) == body


def test_framebuffer_update_request_passes_through() -> None:
    # type 3 + 9 bytes, always forwarded.
    body = _handshake() + bytes([3]) + b"F" * 9
    assert _drive(body) == body


def test_pointer_event_forwarded_when_allowed() -> None:
    # type 5 + 5 bytes, gated on inject like key events.
    body = _handshake() + bytes([5]) + bytes(5)
    assert len(_drive(body, can_inject=lambda *_a: True)) == 14 + 1 + 5


def test_pointer_event_dropped_when_denied() -> None:
    body = _handshake() + bytes([5]) + bytes(5)
    assert _drive(body, can_inject=lambda *_a: False) == _handshake()


def test_cut_text_dropped_when_denied() -> None:
    # Cut-text read but not forwarded when inject denied; filter keeps going.
    text = b"secret"
    body = (
        _handshake()
        + bytes([6])
        + bytes(3)
        + struct.pack("!I", len(text))
        + text
        + bytes([4])
        + bytes(7)  # trailing key event, also denied
    )
    assert _drive(body, can_inject=lambda *_a: False) == _handshake()


def test_cut_text_empty_payload_allowed() -> None:
    # Zero-length cut-text: header forwarded, no payload branch.
    body = _handshake() + bytes([6]) + bytes(3) + struct.pack("!I", 0)
    out = _drive(body, can_inject=lambda *_a: True)
    assert out == _handshake() + bytes([6]) + bytes(3) + struct.pack("!I", 0)


def test_oversized_cut_text_short_read_returns() -> None:
    # Oversized length but the stream ends mid-drain → filter returns cleanly.
    length = (1 << 20) + 100
    body = _handshake() + bytes([6]) + bytes(3) + struct.pack("!I", length) + b"partial"
    assert _drive(body, can_inject=lambda *_a: True) == _handshake()


def test_unknown_message_type_raises() -> None:
    body = _handshake() + bytes([99])
    with pytest.raises(ValueError, match="unknown RFB client message type"):
        _drive(body)


def _fbur() -> bytes:
    # type 3 (FramebufferUpdateRequest) + incremental + x + y + w + h
    return struct.pack(">BBHHHH", 3, 1, 0, 0, 0, 0)


def test_on_client_ready_fires_once_on_first_request() -> None:
    """on_client_ready fires on the client's FIRST FBUR, not on later ones."""
    calls: list[int] = []
    src = io.BytesIO(_handshake() + _fbur() + _fbur())
    dst = io.BytesIO()
    filter_rfb_client_input(
        dst,
        src,
        can_inject=None,
        session_id="s",
        lease_id="l",
        principal_id="p",
        principal_role="operator",
        on_client_ready=lambda: calls.append(1),
    )
    assert calls == [1]  # fired exactly once despite two requests
    # both requests still forwarded upstream: handshake(14) + 2 * FBUR(10)
    assert len(dst.getvalue()) == 14 + 2 * len(_fbur())
