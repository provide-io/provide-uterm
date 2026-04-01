#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Convert animated GIF files to ANSI terminal art frames.

Uses half-block characters (▄) to render two vertical pixels per
terminal cell.  Supports three color modes:

- ``"truecolor"`` — ESC[38;2;R;G;B / ESC[48;2;R;G;B (24-bit)
- ``"256"``       — ESC[38;5;N / ESC[48;5;N (xterm 256-color palette)
- ``"16"``        — ESC[30-37;40-47m (classic 16-color, nearest match)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# 16-color palette (standard ANSI) — (R, G, B, fg_code, bg_code)
# ---------------------------------------------------------------------------

_ANSI16: list[tuple[int, int, int, int, int]] = [
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
# xterm 256-color palette — first 16 match _ANSI16, then 216-color cube + 24 grays
# ---------------------------------------------------------------------------

_XTERM256: list[tuple[int, int, int]] = []


def _build_xterm256() -> None:
    if _XTERM256:
        return
    # Standard 16
    for r, g, b, _fg, _bg in _ANSI16:
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
    return (r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2


# ---------------------------------------------------------------------------
# Quantizers
# ---------------------------------------------------------------------------


def _nearest_16(r: int, g: int, b: int) -> tuple[int, int]:
    """Return (fg_code, bg_code) for the nearest 16-color match."""
    best_i = 0
    best_d = _color_dist_sq(r, g, b, *_ANSI16[0][:3])
    for i in range(1, 16):
        d = _color_dist_sq(r, g, b, *_ANSI16[i][:3])
        if d < best_d:
            best_d = d
            best_i = i
    return _ANSI16[best_i][3], _ANSI16[best_i][4]


def _nearest_256(r: int, g: int, b: int) -> int:
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


# ---------------------------------------------------------------------------
# SGR emitters
# ---------------------------------------------------------------------------


def _sgr_truecolor(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]};48;2;{bg[0]};{bg[1]};{bg[2]}m"


def _sgr_256(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> str:
    fi = _nearest_256(*fg)
    bi = _nearest_256(*bg)
    return f"\x1b[38;5;{fi};48;5;{bi}m"


def _sgr_16(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> str:
    fg_code, _ = _nearest_16(*fg)
    _, bg_code = _nearest_16(*bg)
    return f"\x1b[{fg_code};{bg_code}m"


_SGR_FN = {
    "truecolor": _sgr_truecolor,
    "256": _sgr_256,
    "16": _sgr_16,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ColorMode = Literal["truecolor", "256", "16"]


def gif_to_ansi_frames(
    gif_path: str | Path,
    cols: int = 80,
    rows: int = 40,
    mode: ColorMode = "truecolor",
) -> tuple[list[str], float]:
    """Convert a GIF file to a list of ANSI-escaped terminal frames.

    Uses ▄ (lower half block) with fg=bottom pixel, bg=top pixel
    to render two pixel rows per terminal row.

    Args:
        gif_path: Path to the GIF file.
        cols: Terminal width in columns.
        rows: Terminal height in rows (each row = 2 pixel rows).
        mode: Color mode — ``"truecolor"``, ``"256"``, or ``"16"``.

    Returns:
        (frames, fps) — list of ANSI strings and the source FPS.
    """
    from PIL import Image

    sgr_fn = _SGR_FN[mode]

    img = Image.open(gif_path)
    n_frames = getattr(img, "n_frames", 1)
    duration_ms = img.info.get("duration", 100) or 100
    fps = 1000.0 / duration_ms

    px_w = cols
    px_h = rows * 2

    frames: list[str] = []

    for frame_idx in range(n_frames):
        img.seek(frame_idx)
        frame = img.convert("RGBA").resize((px_w, px_h), Image.NEAREST)
        pixels = frame.load()

        parts: list[str] = ["\x1b[H"]

        for y in range(0, px_h, 2):
            row_parts: list[str] = []
            prev_sgr = ""

            for x in range(px_w):
                tr, tg, tb, ta = pixels[x, y]
                br, bg_val, bb, ba = pixels[x, y + 1] if y + 1 < px_h else (0, 0, 0, 0)

                if ta < 128:
                    tr, tg, tb = 0, 0, 0
                if ba < 128:
                    br, bg_val, bb = 0, 0, 0

                fg = (br, bg_val, bb)
                bg = (tr, tg, tb)

                sgr = sgr_fn(fg, bg)
                if sgr != prev_sgr:
                    row_parts.append(sgr)
                    prev_sgr = sgr

                row_parts.append("▄")

            row_parts.append("\x1b[0m\r\n")
            parts.append("".join(row_parts))

        frames.append("".join(parts))

    return frames, fps
