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
    _nearest_16,
    _nearest_256,
    _sgr_256,
    _sgr_truecolor,
    image_to_ansi_frames,
)

from .test_shell_render_part1 import _fresh_build

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


def test_build_xterm256_idempotent() -> None:
    # Calling twice should not change length (early-return guard)
    rm = _fresh_build()
    rm._build_xterm256()
    assert len(rm._XTERM256) == 256


# ---------------------------------------------------------------------------
# _sgr_truecolor — all 6 channel slots must be distinct
# ---------------------------------------------------------------------------


def test_sgr_truecolor_all_channels_distinct() -> None:
    # Use all different values so any channel swap is caught
    result = _sgr_truecolor((10, 20, 30), (40, 50, 60))
    assert result == "\x1b[38;2;10;20;30;48;2;40;50;60m"


def test_sgr_truecolor_fg_g_not_swapped_with_b() -> None:
    # fg[1] must not be replaced by fg[2]
    result = _sgr_truecolor((1, 2, 3), (4, 5, 6))
    assert "38;2;1;2;3" in result


def test_sgr_truecolor_bg_r_not_swapped_with_g() -> None:
    # bg[0] must not be replaced by bg[1]
    result = _sgr_truecolor((1, 2, 3), (4, 5, 6))
    assert "48;2;4;5;6" in result


# ---------------------------------------------------------------------------
# _sgr_256 — verify actual numeric index values appear in output
# ---------------------------------------------------------------------------


def test_sgr_256_fg_index_correct() -> None:
    # Pure red maps to index 196; the output must contain 38;5;196
    result = _sgr_256((255, 0, 0), (0, 0, 0))
    assert "38;5;196" in result


def test_sgr_256_bg_index_correct() -> None:
    # Black bg maps to index 0; output must contain 48;5;0
    result = _sgr_256((255, 0, 0), (0, 0, 0))
    assert "48;5;0" in result


# ---------------------------------------------------------------------------
# _nearest_16 — index 0 participation and tie-breaking edge cases
# ---------------------------------------------------------------------------


def test_nearest_16_loop_includes_index_0() -> None:
    # (0, 0, 0) is already initialized as best_i=0 before the loop;
    # result must still be correct regardless of loop start.
    fg, bg = _nearest_16(0, 0, 0)
    assert fg == 30
    assert bg == 40


def test_nearest_16_second_entry_is_best() -> None:
    # Index 1 = dark red (170, 0, 0); choose a color closer to it than index 0
    # (160, 0, 0) → dist to index 0 (0,0,0) = 160^2; dist to index 1 (170,0,0) = 100
    fg, bg = _nearest_16(160, 0, 0)
    assert fg == 31
    assert bg == 41


def test_nearest_16_tie_broken_by_strict_less_than() -> None:
    # (85, 0, 0) is equidistant from index 0 (black, dist=85²=7225)
    # and index 1 (dark red (170,0,0), dist=(85-170)²=7225).
    # With strict '<' (original), index 0 wins → fg=30 (black fg).
    # With '<=' (mutmut_28), index 1 wins on the tie → fg=31 (red fg).
    # The correct behavior is to return index 0 (first encountered best).
    fg, bg = _nearest_16(85, 0, 0)
    assert fg == 30  # black fg wins the tie with strict '<'
    assert bg == 40  # black bg


# ---------------------------------------------------------------------------
# _nearest_256 — verify index 1 can be the best match
# ---------------------------------------------------------------------------


def test_nearest_256_index_1_best() -> None:
    # Index 1 = dark red (170, 0, 0) from _ANSI16.
    # A color like (170, 0, 0) is exactly that entry.
    result = _nearest_256(170, 0, 0)
    assert result == 1


def test_nearest_256_loop_covers_index_1() -> None:
    # If range started at 2 (mutmut_16), index 1 would never be considered.
    # (170, 0, 0) exactly matches index 1; it must not return index 0 or 2+.
    result = _nearest_256(170, 0, 0)
    assert result == 1


# ---------------------------------------------------------------------------
# _render_frame — detailed pixel-level checks
# ---------------------------------------------------------------------------


def _make_rgba_png(pixels_rgba: list[list[tuple[int, int, int, int]]]) -> bytes:
    """Build a tiny RGBA PNG from a 2D list of (R,G,B,A) tuples."""
    h = len(pixels_rgba)
    w = len(pixels_rgba[0])
    img = Image.new("RGBA", (w, h))
    img.putdata([px for row in pixels_rgba for px in row])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_render_frame_cursor_home_present() -> None:
    # 2x2 opaque red image — cursor home must be the very first escape
    data = _make_png(2, 2, (255, 0, 0))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    assert frames[0][0:3] == "\x1b[H"


def test_render_frame_uses_bottom_pixel_as_fg() -> None:
    # Top pixel = (0, 0, 255), Bottom pixel = (255, 0, 0)
    # fg = bottom = (255,0,0) → 38;2;255;0;0
    # bg = top    = (0,0,255) → 48;2;0;0;255
    pixels = [
        [(0, 0, 255, 255)],  # top row (y=0) → bg
        [(255, 0, 0, 255)],  # bottom row (y=1) → fg
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "38;2;255;0;0" in frames[0]
    assert "48;2;0;0;255" in frames[0]


def test_render_frame_fg_bg_not_swapped() -> None:
    # Verify fg (bottom pixel) and bg (top pixel) are not swapped.
    # Top row: pure green (0,255,0); Bottom row: pure blue (0,0,255).
    pixels = [
        [(0, 255, 0, 255)],  # top → bg
        [(0, 0, 255, 255)],  # bottom → fg
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "38;2;0;0;255" in frames[0]  # fg = blue (bottom)
    assert "48;2;0;255;0" in frames[0]  # bg = green (top)


def test_render_frame_transparent_top_pixel_zeroed() -> None:
    # Top pixel: transparent red (255,0,0,0) → alpha < 128 → bg should become 0,0,0
    # Bottom pixel: opaque blue (0,0,255,255)
    pixels = [
        [(255, 0, 0, 0)],  # transparent — should be treated as (0,0,0)
        [(0, 0, 255, 255)],  # opaque
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    # bg (from zeroed top) = 0,0,0
    assert "48;2;0;0;0" in frames[0]


def test_render_frame_transparent_bottom_pixel_zeroed() -> None:
    # Bottom pixel transparent — fg should become (0,0,0)
    pixels = [
        [(0, 255, 0, 255)],  # opaque green top
        [(255, 0, 0, 0)],  # transparent bottom → zeroed → fg = (0,0,0)
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    # fg (from zeroed bottom) = 0,0,0
    assert "38;2;0;0;0" in frames[0]


def test_render_frame_alpha_threshold_127_transparent() -> None:
    # Alpha = 127 (< 128) → zeroed
    pixels = [
        [(200, 100, 50, 127)],  # alpha=127 → transparent
        [(0, 0, 0, 255)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "48;2;0;0;0" in frames[0]


def test_render_frame_alpha_threshold_128_opaque() -> None:
    # Alpha = 128 (>= 128) → not zeroed
    pixels = [
        [(200, 100, 50, 128)],  # alpha=128 → opaque
        [(0, 0, 0, 255)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    # bg = (200, 100, 50) not zeroed
    assert "48;2;200;100;50" in frames[0]


def test_render_frame_odd_height_last_row_padding() -> None:
    # When px_h is odd, the last row's bottom pixel uses the (0,0,0,0) fallback.
    # Call _render_frame directly with px_h=1 to force the odd-height code path.
    from provide.uterm.shell._render import _render_frame
    from provide.uterm.shell._render import _sgr_truecolor as sgr_fn

    class FakePixels:
        def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int, int]:
            x, y = xy
            return (0, 255, 0, 255)  # always green

    # px_h=1 → loop runs once (y=0); y+1=1 >= px_h=1 → fallback (0,0,0,0)
    result = _render_frame(FakePixels(), 1, 1, sgr_fn)
    # fg = fallback (0,0,0), bg = (0,255,0)
    assert "38;2;0;0;0" in result
    assert "48;2;0;255;0" in result


def test_render_frame_sgr_deduplication() -> None:
    # 2 pixels of the same color → SGR should only appear once per row (dedup)
    data = _make_png(2, 2, (255, 0, 0))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    # The same SGR should not be repeated
    frame = frames[0]
    # Strip cursor home and count distinct SGR segments — should be 1 per uniform row
    after_home = frame[3:]  # strip \x1b[H
    assert after_home.count("\x1b[38;2;") == 1


def test_render_frame_half_block_count_matches_pixels() -> None:
    # 4×2 image → cols=4, rows=1 → 4 half-block chars in output
    data = _make_png(4, 2, (0, 128, 0))
    frames, _ = image_to_ansi_frames(data, cols=4, rows=1, mode="truecolor")
    assert frames[0].count("▄") == 4


def test_render_frame_row_ends_with_reset_and_crlf() -> None:
    data = _make_png(2, 2, (0, 0, 128))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    # Each row must end with reset + CRLF
    assert "\x1b[0m\r\n" in frames[0]


def test_render_frame_return_string_joined() -> None:
    # Ensure the frame is a plain str (not list) and rows are joined without separator
    data = _make_png(2, 4, (128, 0, 128))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=2, mode="truecolor")
    assert isinstance(frames[0], str)
    # "XXXX".join would insert XXXX between rows — verify it's not there
    assert "XXXX" not in frames[0]


# ---------------------------------------------------------------------------
# image_to_ansi_frames — default parameter and attribute coverage
# ---------------------------------------------------------------------------


def test_default_cols_80() -> None:
    # With default cols=80, the output should contain exactly 80*24 half-blocks
    data = _make_png(80, 48, (0, 200, 0))
    frames, _ = image_to_ansi_frames(data)  # cols=80 default
    assert frames[0].count("▄") == 80 * 24


def test_default_rows_24() -> None:
    data = _make_png(4, 4, (0, 0, 200))
    frames, _ = image_to_ansi_frames(data, cols=4)  # rows=24 default
    assert frames[0].count("\x1b[0m\r\n") == 24


def test_static_image_n_frames_fallback_to_1() -> None:
    # A static PNG has no n_frames attribute; getattr default=1 must be used
    data = _make_png()
    frames, fps = image_to_ansi_frames(data)
    assert len(frames) == 1
    assert fps == 0.0


def test_duration_zero_gives_fps_zero() -> None:
    # Static PNG has no duration info → fps must be 0.0 (not division by zero)
    data = _make_png()
    _, fps = image_to_ansi_frames(data)
    assert fps == 0.0


def test_fps_calculation_100ms_duration() -> None:
    # GIF with duration=100ms → 1000/100 = 10.0 fps
    data = _make_animated_gif(n_frames=2)
    _, fps = image_to_ansi_frames(data)
    assert abs(fps - 10.0) < 0.01


def test_fps_calculation_exact_value() -> None:
    # Verify fps = 1000.0 / duration_ms exactly (not 1001.0 or other mutation)
    data = _make_animated_gif(n_frames=2)
    _, fps = image_to_ansi_frames(data)
    # duration=100ms → fps=10.0 (not 10.01 from 1001/100)
    assert fps == pytest.approx(10.0)


def test_lanczos_resize_produces_correct_dimensions() -> None:
    # Verify the resize actually lands at cols x (rows*2); a wrong filter arg
    # (e.g. None) would still succeed in PIL but we can verify the terminal row count.
    data = _make_png(200, 200, (100, 150, 200))
    frames, _ = image_to_ansi_frames(data, cols=10, rows=5)
    # 5 terminal rows → 5 reset+CRLF sequences
    assert frames[0].count("\x1b[0m\r\n") == 5
    # 10 cols → 10 half-blocks per row
    assert frames[0].count("▄") == 10 * 5


# ---------------------------------------------------------------------------
# Additional tests to kill surviving mutants
# ---------------------------------------------------------------------------


def _make_multicolor_png(width: int, height: int) -> bytes:
    """Build a PNG where each row has a distinct color."""
    img = Image.new("RGBA", (width, height))
    pixels = img.load()
    colors = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 0, 255)]
    for y in range(height):
        for x in range(width):
            pixels[x, y] = colors[y % len(colors)]
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_gif_with_duration(duration_ms: int, n_frames: int = 2) -> bytes:
    """Build an animated GIF with a specific per-frame duration."""
    frames = [Image.new("RGB", (4, 4), (i * 80, 255 - i * 80, 128)) for i in range(n_frames)]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0,
    )
    return buf.getvalue()


def test_render_frame_bottom_pixel_alpha_128_is_opaque() -> None:
    # Bottom pixel alpha=128 (≥ 128) must NOT be zeroed.
    # Mutant_34: ba <= 128 would zero alpha=128 pixels (making fg black instead of blue).
    # Mutant_35: ba < 129 would also zero alpha=128 pixels.
    pixels = [
        [(0, 255, 0, 255)],  # top: opaque green
        [(0, 0, 200, 128)],  # bottom: alpha=128 → opaque (not zeroed)
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    # fg = bottom pixel = (0, 0, 200), not zeroed
    assert "38;2;0;0;200" in frames[0]
