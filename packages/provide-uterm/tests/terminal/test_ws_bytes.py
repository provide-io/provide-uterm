#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the lossless byte ↔ str shim used by the inline control channel."""

from __future__ import annotations

import pytest

from provide.uterm.ws_bytes import channel_str_to_bytes, ws_frame_to_channel_str


def test_ws_frame_str_passthrough() -> None:
    assert ws_frame_to_channel_str("hello") == "hello"


def test_ws_frame_bytes_to_channel_str_is_lossless_across_all_bytes() -> None:
    raw = bytes(range(256))
    shimmed = ws_frame_to_channel_str(raw)
    assert channel_str_to_bytes(shimmed) == raw


def test_roundtrip_preserves_cp437_high_bytes() -> None:
    """The CP437 byte range 0x80-0xFF must survive the shim verbatim.

    Regression: prior code re-encoded the data segment as cp437 with
    errors='replace', which silently replaced bytes 0x80-0x9F with '?'
    because cp437 has no codepoint for U+0080-U+009F (those high-bit
    glyphs map to other codepoints in cp437, not to themselves).
    """
    cp437_high = bytes(range(0x80, 0x100))
    shimmed = ws_frame_to_channel_str(cp437_high)
    recovered = channel_str_to_bytes(shimmed)
    assert recovered == cp437_high


def test_cp437_roundtrip_would_have_lost_bytes() -> None:
    """Documents *why* we use latin-1 and not cp437 for the shim."""
    cp437_high = bytes(range(0x80, 0x100))
    shimmed = ws_frame_to_channel_str(cp437_high)
    via_cp437 = shimmed.encode("cp437", errors="replace")
    assert via_cp437 != cp437_high
    assert b"?" in via_cp437


def test_ws_frame_bytes_outside_latin1_would_raise() -> None:
    """Binary frames cannot contain codepoints — but if a sender broke
    the contract and passed a str containing a non-latin-1 codepoint
    into the data segment, the recovery step replaces rather than
    raises, since the caller is mid-stream."""
    out = channel_str_to_bytes("a☃b")
    assert out == b"a?b"


def test_ws_frame_strict_decode_on_bytes() -> None:
    """ws_frame_to_channel_str uses strict decode on bytes because
    latin-1 cannot fail for any byte sequence — but we want to surface
    bugs if someone ever swaps the codec."""
    raw = bytes(range(256))
    assert ws_frame_to_channel_str(raw) == raw.decode("latin-1")


def test_imports_from_package_root() -> None:
    """Helpers must be exposed at the ``provide.uterm`` root for callers."""
    from provide.uterm import channel_str_to_bytes as a
    from provide.uterm import ws_frame_to_channel_str as b

    assert a is channel_str_to_bytes
    assert b is ws_frame_to_channel_str


@pytest.mark.parametrize("payload", [b"", b"\x00", b"\xff", b"\x80\x9f\xa0"])
def test_roundtrip_param(payload: bytes) -> None:
    assert channel_str_to_bytes(ws_frame_to_channel_str(payload)) == payload
