#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Image-to-ANSI terminal art conversion.

Uses half-block characters (lower-half block U+2584) to render two vertical
pixels per terminal cell.  Supports three color modes:

- ``"truecolor"`` — ESC[38;2;R;G;B / ESC[48;2;R;G;B (24-bit)
- ``"256"``       — ESC[38;5;N / ESC[48;5;N (xterm 256-color palette)
- ``"16"``        — ESC[30-37;40-47m (classic 16-color, nearest match)

Requires the ``[images]`` extra (``pip install 'provide-terminal-render[images]'``).
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from provide.terminal.render.sgr import SGR_FUNCTIONS, ColorMode

if TYPE_CHECKING:
    from collections.abc import Callable


def render_frame(
    pixels: Any,  # PIL PixelAccess object
    px_w: int,
    px_h: int,
    sgr_fn: Callable[[tuple[int, int, int], tuple[int, int, int]], str],
) -> str:
    """Render a single ANSI frame from pixel data.

    Uses lower-half block with fg=bottom pixel, bg=top pixel
    to render two pixel rows per terminal row.

    Args:
        pixels: PIL PixelAccess object from frame.load().
        px_w: Pixel width.
        px_h: Pixel height.
        sgr_fn: SGR emitter function for the chosen color mode.

    Returns:
        ANSI-escaped string for the frame.
    """
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

            row_parts.append("\u2584")

        row_parts.append("\x1b[0m\r\n")
        parts.append("".join(row_parts))

    return "".join(parts)


def image_to_ansi_frames(
    data: bytes,
    cols: int = 80,
    rows: int = 24,
    mode: ColorMode = "truecolor",
) -> tuple[list[str], float]:
    """Convert raw image bytes to a list of ANSI-escaped terminal frames.

    Supports any format PIL can open: PNG, JPEG, GIF, WebP, etc.
    Animated formats (GIF, APNG, WebP) yield multiple frames.

    Args:
        data: Raw image bytes.
        cols: Terminal width in columns.
        rows: Terminal height in rows (each row = 2 pixel rows).
        mode: Color mode — ``"truecolor"``, ``"256"``, or ``"16"``.

    Returns:
        (frames, fps) — list of ANSI strings and the source FPS (0.0 for static).
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "missing dependency — Pillow\ninstall the images extra: pip install 'provide-terminal-render[images]'"
        ) from exc

    sgr_fn = SGR_FUNCTIONS[mode]

    img = Image.open(io.BytesIO(data))
    n_frames = getattr(img, "n_frames", 1)
    duration_ms = img.info.get("duration", 0) or 0
    fps = 1000.0 / duration_ms if duration_ms > 0 else 0.0

    px_w = cols
    px_h = rows * 2

    frames: list[str] = []

    for frame_idx in range(n_frames):
        img.seek(frame_idx)
        frame = img.convert("RGBA").resize((px_w, px_h), Image.LANCZOS)
        pixels = frame.load()
        frames.append(render_frame(pixels, px_w, px_h, sgr_fn))

    return frames, fps
