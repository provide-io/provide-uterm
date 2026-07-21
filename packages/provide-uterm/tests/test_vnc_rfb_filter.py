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


def test_cut_text_too_large() -> None:
    # handshake + type 6 + padding3 + length
    length = (1 << 20) + 1
    body = _handshake() + bytes([6]) + bytes(3) + struct.pack("!I", length)
    src = io.BytesIO(body)
    dst = io.BytesIO()
    with pytest.raises(ValueError, match="too large"):
        filter_rfb_client_input(
            dst,
            src,
            can_inject=lambda *_a: True,
            session_id="s",
            lease_id="l",
            principal_id="p",
            principal_role="admin",
        )
