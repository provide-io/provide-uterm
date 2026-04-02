#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI color palette tables and nearest-color quantizers.

Provides the standard 16-color ANSI palette, the xterm 256-color palette
(lazily built), and functions to find the nearest palette entry for an
arbitrary (R, G, B) triple.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 16-color palette (standard ANSI) — (R, G, B, fg_code, bg_code)
# ---------------------------------------------------------------------------

ANSI16_PALETTE: list[tuple[int, int, int, int, int]] = [
    (0, 0, 0, 30, 40),
    (170, 0, 0, 31, 41),
    (0, 170, 0, 32, 42),
    (170, 85, 0, 33, 43),
    (0, 0, 170, 34, 44),
    (170, 0, 170, 35, 45),
    (0, 170, 170, 36, 46),
    (170, 170, 170, 37, 47),
    (85, 85, 85, 90, 100),
    (255, 85, 85, 91, 101),
    (85, 255, 85, 92, 102),
    (255, 255, 85, 93, 103),
    (85, 85, 255, 94, 104),
    (255, 85, 255, 95, 105),
    (85, 255, 255, 96, 106),
    (255, 255, 255, 97, 107),
]

# ---------------------------------------------------------------------------
# xterm 256-color palette — first 16 match ANSI16_PALETTE, then 216-color
# cube + 24 grays
# ---------------------------------------------------------------------------

_XTERM256: list[tuple[int, int, int]] = []


def _build_xterm256() -> None:
    if _XTERM256:
        return
    # Standard 16
    for r, g, b, _fg, _bg in ANSI16_PALETTE:
        _XTERM256.append((r, g, b))
    # 216-color cube (indices 16-231)
    for ri in range(6):
        for gi in range(6):
            for bi in range(6):
                r = 0 if ri == 0 else 55 + 40 * ri
                g = 0 if gi == 0 else 55 + 40 * gi
                b = 0 if bi == 0 else 55 + 40 * bi
                _XTERM256.append((r, g, b))
    # 24 grayscale (indices 232-255)
    for i in range(24):
        v = 8 + 10 * i
        _XTERM256.append((v, v, v))


def _color_dist_sq(r1: int, g1: int, b1: int, r2: int, g2: int, b2: int) -> int:
    """Squared Euclidean distance between two RGB colors."""
    return (r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2


# ---------------------------------------------------------------------------
# Quantizers
# ---------------------------------------------------------------------------


def nearest_16(r: int, g: int, b: int) -> tuple[int, int]:
    """Return (fg_code, bg_code) for the nearest 16-color match."""
    best_i = 0
    best_d = _color_dist_sq(r, g, b, *ANSI16_PALETTE[0][:3])
    for i in range(1, 16):
        d = _color_dist_sq(r, g, b, *ANSI16_PALETTE[i][:3])
        if d < best_d:
            best_d = d
            best_i = i
    return ANSI16_PALETTE[best_i][3], ANSI16_PALETTE[best_i][4]


def nearest_256(r: int, g: int, b: int) -> int:
    """Return the xterm 256-color index for the nearest match."""
    _build_xterm256()
    best_i = 0
    best_d = _color_dist_sq(r, g, b, *_XTERM256[0])
    for i in range(1, 256):
        d = _color_dist_sq(r, g, b, *_XTERM256[i])
        if d < best_d:
            best_d = d
            best_i = i
    return best_i
