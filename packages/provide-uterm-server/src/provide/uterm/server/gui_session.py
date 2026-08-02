#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Graphical console session + minimal RGBA→PNG encoder.

Python reference port of the C# canonical
(``packages/provide-uterm-csharp/src/Provide.Uterm/Gui/{Session,Png}.cs``)
and the Go port (``packages/provide-uterm-go/gui/``).

A :class:`GraphicalSession` is an active connection to a remote graphical
console: it can capture a screenshot (an :class:`RgbaImage`) and inject
pointer / key events. :class:`MemoryGraphicalSession` is an in-memory stub
used for the ``memory`` graphical-target protocol (tests + offline tooling);
the ``rfb`` (VNC) client is deferred. :func:`encode_rgba_png` serialises a
raw RGBA8888 framebuffer to a PNG byte stream with no third-party image
dependency (``zlib`` for the IDAT stream, ``binascii.crc32`` for chunk CRCs).
"""

from __future__ import annotations

import binascii
import struct
import zlib
from typing import Protocol, runtime_checkable

# Hard cap on a single framebuffer dimension (hostile ServerInit protection).
MAX_DIMENSION = 8192


class RgbaImage:
    """RGBA8888 pixel framebuffer (portable, no imaging dependency)."""

    __slots__ = ("_pixels", "height", "width")

    def __init__(self, width: int, height: int, pixels: bytearray | bytes | None = None) -> None:
        if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise ValueError(f"invalid framebuffer dimensions: {width}x{height} (max {MAX_DIMENSION})")
        expected = width * height * 4
        self.width = width
        self.height = height
        if pixels is None:
            self._pixels = bytearray(expected)
        else:
            if len(pixels) != expected:
                raise ValueError(f"pixel buffer length {len(pixels)} does not match {width}x{height} RGBA ({expected})")
            self._pixels = bytearray(pixels)

    @property
    def pixels(self) -> bytearray:
        """The mutable RGBA byte buffer (row-major, 4 bytes/pixel)."""
        return self._pixels

    def clone(self) -> RgbaImage:
        """Return a deep copy (independent pixel buffer)."""
        return RgbaImage(self.width, self.height, bytes(self._pixels))


@runtime_checkable
class GraphicalSession(Protocol):
    """Active connection to a remote graphical console."""

    def screenshot(self) -> RgbaImage:
        """Capture the current console framebuffer."""
        ...

    def inject_pointer(self, x: int, y: int, button_mask: int) -> None:
        """Move the pointer to ``(x, y)`` and set the button bitmask."""
        ...

    def inject_key(self, key_sym: int, down: bool) -> None:
        """Send an X11 keysym press (``down=True``) or release."""
        ...


class MemoryGraphicalSession:
    """In-memory graphical session stub (``memory`` protocol)."""

    __slots__ = ("_fb",)

    def __init__(self, width: int = 640, height: int = 480) -> None:
        self._fb = RgbaImage(width, height)

    def screenshot(self) -> RgbaImage:
        return self._fb.clone()

    def inject_pointer(self, x: int, y: int, button_mask: int) -> None:
        if (button_mask & 1) == 0 or x < 0 or y < 0 or x >= self._fb.width or y >= self._fb.height:
            return
        idx = ((y * self._fb.width) + x) * 4
        self._fb.pixels[idx] = 255
        self._fb.pixels[idx + 1] = 255
        self._fb.pixels[idx + 2] = 255
        self._fb.pixels[idx + 3] = 255

    def inject_key(self, key_sym: int, down: bool) -> None:
        # Stub: memory sessions have no keyboard-driven framebuffer effects.
        _ = (key_sym, down)


def encode_rgba_png(width: int, height: int, pixels: bytes | bytearray) -> bytes:
    """Encode raw RGBA8888 pixels as a PNG byte stream.

    ``pixels`` is row-major, 4 bytes/pixel; extra trailing bytes are ignored.
    """
    if width <= 0 or height <= 0:
        raise ValueError("invalid PNG dimensions")
    expected = width * height * 4
    if len(pixels) < expected:
        raise ValueError(f"pixel buffer too short: need {expected}, got {len(pixels)}")

    # Filter-type-0 (None) row prefixes + raw RGBA scanlines.
    row_len = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        src = y * row_len
        raw += pixels[src : src + row_len]

    # A full zlib stream (0x78 header + deflate + adler32) is exactly the PNG
    # IDAT payload.
    #
    # Z_RLE, not the default strategy, because this stream is a cross-language
    # contract: the corpus is recorded here and the TypeScript and C# ports must
    # reproduce it byte for byte. zlib's default match-finding is not the same
    # in every zlib build -- node ships one on Linux that encodes a 1x1 white
    # pixel in 13 bytes where CPython's takes 11 -- and Z_FIXED does not fix it.
    # Z_RLE constrains matching to distance-1 runs, which every implementation
    # does identically, so the output is stable across languages and platforms.
    # It costs almost nothing here: screenshots are runs, so a blank 640x480
    # frame is byte-identical in size and a white one is 1.06x.
    compressor = zlib.compressobj(9, zlib.DEFLATED, 15, 8, zlib.Z_RLE)
    idat = compressor.compress(bytes(raw)) + compressor.flush()

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    out = bytearray(b"\x89PNG\r\n\x1a\n")
    _write_chunk(out, b"IHDR", ihdr)
    _write_chunk(out, b"IDAT", idat)
    _write_chunk(out, b"IEND", b"")
    return bytes(out)


def _write_chunk(out: bytearray, chunk_type: bytes, data: bytes) -> None:
    out += struct.pack(">I", len(data))
    out += chunk_type
    out += data
    out += struct.pack(">I", binascii.crc32(chunk_type + data) & 0xFFFFFFFF)
