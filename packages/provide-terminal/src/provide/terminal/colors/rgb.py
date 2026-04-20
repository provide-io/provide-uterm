#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""RGB-to-palette-index mapping helpers.

Given an (R, G, B) triple in the 0-255 range, return the nearest palette
index for either the xterm-256 cube (``rgb_to_256``) or the base 16-color
ANSI palette (``rgb_to_16_index``).
"""

from __future__ import annotations

# BBS-canonical 16-color reference palette. Values match typical xterm /
# PuTTY defaults (index 0 = black, 15 = bright white). Euclidean distance
# in RGB space is used to find the nearest index.
_PALETTE_16: list[tuple[int, int, int]] = [
    (0, 0, 0),
    (0, 0, 205),
    (0, 205, 0),
    (0, 205, 205),
    (205, 0, 0),
    (205, 0, 205),
    (205, 205, 0),
    (229, 229, 229),
    (127, 127, 127),
    (92, 92, 255),
    (92, 255, 92),
    (92, 255, 255),
    (255, 92, 92),
    (255, 92, 255),
    (255, 255, 92),
    (255, 255, 255),
]


def _clamp8(v: int) -> int:
    """Clamp an integer to the 0-255 range."""
    if v < 0:
        return 0
    if v > 255:
        return 255
    return v


def rgb_to_256(r: int, g: int, b: int) -> int:
    """Map an (R, G, B) triple to the nearest xterm-256 palette index.

    Uses the standard 6x6x6 color cube (indices 16-231) plus the 24-step
    greyscale ramp (indices 232-255). When R == G == B the greyscale ramp
    is preferred for finer luminance resolution.

    Args:
        r: Red component, 0-255.
        g: Green component, 0-255.
        b: Blue component, 0-255.

    Returns:
        xterm-256 palette index (16-255).
    """
    rr, gg, bb = _clamp8(r), _clamp8(g), _clamp8(b)
    if rr == gg == bb:
        if rr < 8:
            return 16
        if rr > 248:
            return 231
        return 232 + int((rr - 8) / 247 * 24)
    rc = round(rr / 255 * 5)
    gc = round(gg / 255 * 5)
    bc = round(bb / 255 * 5)
    return 16 + 36 * rc + 6 * gc + bc


def rgb_to_16_index(r: int, g: int, b: int) -> int:
    """Map an (R, G, B) triple to the nearest base-16 ANSI palette index.

    Uses Euclidean distance in RGB space against the reference palette
    (xterm/PuTTY defaults). Returns an index 0-15 where 0-7 are the
    normal colors and 8-15 are the bright variants.

    Args:
        r: Red component, 0-255.
        g: Green component, 0-255.
        b: Blue component, 0-255.

    Returns:
        Palette index 0-15.
    """
    rr, gg, bb = _clamp8(r), _clamp8(g), _clamp8(b)
    best_i, best_d = 0, 10**9
    for i, (tr, tg, tb) in enumerate(_PALETTE_16):
        d = (rr - tr) * (rr - tr) + (gg - tg) * (gg - tg) + (bb - tb) * (bb - tb)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


__all__ = ["rgb_to_16_index", "rgb_to_256"]
