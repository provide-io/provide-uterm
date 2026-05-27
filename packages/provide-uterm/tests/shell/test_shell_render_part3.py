#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.shell._render — ANSI image rendering."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from provide.uterm.shell._render import (
    image_to_ansi_frames,
)

from .test_shell_render_part2 import _make_gif_with_duration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png(width: int = 4, height: int = 4, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_transparent_png(width: int = 4, height: int = 4) -> bytes:
    img = Image.new("RGBA", (width, height), (255, 0, 0, 0))  # fully transparent
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_animated_gif(n_frames: int = 3, size: tuple[int, int] = (4, 4)) -> bytes:
    frames = [Image.new("RGB", size, (i * 80, 255 - i * 80, 128)) for i in range(n_frames)]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _sgr_truecolor
# ---------------------------------------------------------------------------


def test_render_frame_y_plus_1_not_y_minus_1() -> None:
    # mutmut_19 changes y+1 to y-1; at y=2 in a 4-row image:
    #   correct:  pixels[x, 3] (row 3 = yellow, fg=yellow, bg=blue)
    #   mutant:   pixels[x, 1] (row 1 = green, fg=green, bg=blue)
    # Verify that the combined SGR for row 1 (y=2) uses yellow fg + blue bg,
    # not green fg + blue bg.
    from provide.uterm.shell._render import _render_frame
    from provide.uterm.shell._render import _sgr_truecolor as sgr_fn

    row_colors = [
        (255, 0, 0, 255),  # y=0: red   → bg for terminal row 0
        (0, 255, 0, 255),  # y=1: green → fg for terminal row 0
        (0, 0, 255, 255),  # y=2: blue  → bg for terminal row 1
        (255, 255, 0, 255),  # y=3: yellow → fg for terminal row 1 (correct: y+1=3)
    ]

    class FakePixels4:
        def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int, int]:
            x, y = xy
            return row_colors[y % len(row_colors)]

    result = _render_frame(FakePixels4(), 1, 4, sgr_fn)
    # Terminal row 1 (y=2 in pixel space): fg=yellow(255,255,0), bg=blue(0,0,255)
    # The truecolor SGR for this cell must be the exact combined sequence.
    assert "38;2;255;255;0;48;2;0;0;255" in result  # yellow fg + blue bg (correct)
    assert "38;2;0;255;0;48;2;0;0;255" not in result  # not green fg + blue bg (mutant)


def test_render_frame_row_ends_exactly_with_reset_crlf() -> None:
    # mutmut_53 changes '\x1b[0m\r\n' to 'XX\x1b[0m\r\nXX'
    # Check that the frame ends with '\x1b[0m\r\n' (no trailing XX).
    data = _make_png(2, 2, (0, 0, 128))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    assert frames[0].endswith("\x1b[0m\r\n")


def test_fps_calculation_10ms_duration() -> None:
    # mutmut_37: duration_ms > 1 instead of duration_ms > 0.
    # GIF minimum frame duration is 10ms; a GIF with duration=10ms gives fps=100.0.
    # Both original (10 > 0) and mutant (10 > 1) are True, so this test covers the
    # main fps formula path; the boundary at duration_ms=1 is unreachable via real GIFs.
    data = _make_gif_with_duration(10, n_frames=2)
    _, fps = image_to_ansi_frames(data)
    assert fps == pytest.approx(100.0)


def test_lanczos_produces_expected_pixel_values() -> None:
    # mutmut_48: Image.LANCZOS → None (PIL default=BICUBIC); mutmut_50: no arg (also BICUBIC)
    # LANCZOS and BICUBIC produce measurably different pixels when downsampling a gradient.
    # Build a 100x100 gradient and resize to 4x2; verify a specific LANCZOS pixel appears.
    img = Image.new("RGBA", (100, 100))
    pix = img.load()
    for yy in range(100):
        for xx in range(100):
            pix[xx, yy] = (xx * 2 % 256, yy * 2 % 256, (xx + yy) % 256, 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    # Compute expected LANCZOS pixel at position [0, 0] (the top-left pixel → bg of row 0 col 0)
    expected_img = Image.open(io.BytesIO(data)).convert("RGBA").resize((4, 2), Image.LANCZOS)
    expected_bg = expected_img.getpixel((0, 0))[:3]  # bg = top pixel

    frames, _ = image_to_ansi_frames(data, cols=4, rows=1, mode="truecolor")
    # The bg (top pixel at x=0,y=0) must match LANCZOS output
    expected_bg_sgr = f"48;2;{expected_bg[0]};{expected_bg[1]};{expected_bg[2]}"
    assert expected_bg_sgr in frames[0]

    # Verify LANCZOS != BICUBIC for this image (confirming the test is meaningful)
    bicubic_img = Image.open(io.BytesIO(data)).convert("RGBA").resize((4, 2), None)
    bicubic_bg = bicubic_img.getpixel((0, 0))[:3]
    assert expected_bg != bicubic_bg, "LANCZOS and BICUBIC must differ for this test to be meaningful"
