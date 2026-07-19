#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the graphical session stub + RGBA→PNG encoder.

Covers :class:`RgbaImage`, :class:`MemoryGraphicalSession`, and
:func:`encode_rgba_png` (signature, chunk structure, zlib round-trip, and
every validation branch).
"""

from __future__ import annotations

import struct
import zlib

import pytest

from provide.uterm.server.gui_session import (
    MAX_DIMENSION,
    GraphicalSession,
    MemoryGraphicalSession,
    RgbaImage,
    encode_rgba_png,
)

# ---------------------------------------------------------------------------
# RgbaImage
# ---------------------------------------------------------------------------


class TestRgbaImage:
    def test_default_zero_buffer(self) -> None:
        img = RgbaImage(2, 3)
        assert img.width == 2
        assert img.height == 3
        assert img.pixels == bytearray(2 * 3 * 4)

    def test_with_pixels(self) -> None:
        buf = bytes(range(2 * 2 * 4))
        img = RgbaImage(2, 2, buf)
        assert bytes(img.pixels) == buf

    def test_clone_is_independent(self) -> None:
        img = RgbaImage(1, 1)
        clone = img.clone()
        clone.pixels[0] = 42
        assert img.pixels[0] == 0

    @pytest.mark.parametrize(
        "w,h",
        [(0, 4), (4, 0), (-1, 4), (4, -1), (MAX_DIMENSION + 1, 4), (4, MAX_DIMENSION + 1)],
    )
    def test_bad_dimensions(self, w: int, h: int) -> None:
        with pytest.raises(ValueError, match="invalid framebuffer dimensions"):
            RgbaImage(w, h)

    def test_pixel_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="does not match"):
            RgbaImage(2, 2, b"\x00\x00")


# ---------------------------------------------------------------------------
# MemoryGraphicalSession
# ---------------------------------------------------------------------------


class TestMemoryGraphicalSession:
    def test_is_graphical_session(self) -> None:
        assert isinstance(MemoryGraphicalSession(), GraphicalSession)

    def test_screenshot_default_dims(self) -> None:
        img = MemoryGraphicalSession().screenshot()
        assert (img.width, img.height) == (640, 480)

    def test_screenshot_returns_clone(self) -> None:
        sess = MemoryGraphicalSession(2, 2)
        a = sess.screenshot()
        a.pixels[0] = 9
        b = sess.screenshot()
        assert b.pixels[0] == 0

    def test_inject_pointer_sets_white_pixel(self) -> None:
        sess = MemoryGraphicalSession(4, 4)
        sess.inject_pointer(2, 1, 1)
        img = sess.screenshot()
        idx = ((1 * 4) + 2) * 4
        assert list(img.pixels[idx : idx + 4]) == [255, 255, 255, 255]

    @pytest.mark.parametrize(
        "x,y,mask",
        [(-1, 0, 1), (0, -1, 1), (4, 0, 1), (0, 4, 1), (1, 1, 0), (1, 1, 2)],
    )
    def test_inject_pointer_noop(self, x: int, y: int, mask: int) -> None:
        sess = MemoryGraphicalSession(4, 4)
        sess.inject_pointer(x, y, mask)
        assert sess.screenshot().pixels == bytearray(4 * 4 * 4)

    def test_inject_key_is_noop(self) -> None:
        sess = MemoryGraphicalSession(2, 2)
        sess.inject_key(0xFF0D, True)
        sess.inject_key(0xFF0D, False)
        assert sess.screenshot().pixels == bytearray(2 * 2 * 4)


# ---------------------------------------------------------------------------
# encode_rgba_png
# ---------------------------------------------------------------------------


def _parse_png(png: bytes) -> tuple[tuple[int, int, int, int], bytes]:
    """Return ((w, h, bit_depth, color_type), decompressed_raw)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    off = 8
    ihdr: tuple[int, int, int, int] | None = None
    idat = b""
    while off < len(png):
        length = struct.unpack(">I", png[off : off + 4])[0]
        ctype = png[off + 4 : off + 8]
        data = png[off + 8 : off + 8 + length]
        if ctype == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", data[:10])
            ihdr = (w, h, bd, ct)
        elif ctype == b"IDAT":
            idat += data
        off += 12 + length
    assert ihdr is not None
    return ihdr, zlib.decompress(idat)


class TestEncodePng:
    def test_header_and_dims(self) -> None:
        img = RgbaImage(4, 3)
        (w, h, bd, ct), _ = _parse_png(encode_rgba_png(img.width, img.height, img.pixels))
        assert (w, h, bd, ct) == (4, 3, 8, 6)

    def test_roundtrip_pixels(self) -> None:
        sess = MemoryGraphicalSession(4, 3)
        sess.inject_pointer(3, 2, 1)
        img = sess.screenshot()
        _, raw = _parse_png(encode_rgba_png(img.width, img.height, img.pixels))
        # Each scanline is 1 filter byte + width*4 pixel bytes.
        assert len(raw) == 3 * (1 + 4 * 4)
        row2 = raw[2 * (1 + 16) : 2 * (1 + 16) + 1 + 16]
        assert row2[0] == 0  # filter None
        assert list(row2[1 + 3 * 4 : 1 + 3 * 4 + 4]) == [255, 255, 255, 255]

    def test_ignores_trailing_pixels(self) -> None:
        # A buffer longer than w*h*4 is accepted; extra bytes are ignored.
        png = encode_rgba_png(1, 1, bytes(4 + 9))
        (w, h, _, _), raw = _parse_png(png)
        assert (w, h) == (1, 1)
        assert len(raw) == 1 + 4

    @pytest.mark.parametrize("w,h", [(0, 1), (1, 0), (-1, 1)])
    def test_bad_dimensions(self, w: int, h: int) -> None:
        with pytest.raises(ValueError, match="invalid PNG dimensions"):
            encode_rgba_png(w, h, b"\x00\x00\x00\x00")

    def test_buffer_too_short(self) -> None:
        with pytest.raises(ValueError, match="pixel buffer too short"):
            encode_rgba_png(2, 2, b"\x00\x00")
