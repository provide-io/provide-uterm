#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.shell._render — ANSI image rendering."""

from __future__ import annotations

import importlib
import io
import sys
from collections.abc import Generator

import pytest
from PIL import Image

from provide.terminal.shell._render import (
    _color_dist_sq,
    _nearest_16,
    _nearest_256,
    _sgr_16,
    _sgr_256,
    _sgr_truecolor,
    image_to_ansi_frames,
)

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


def test_sgr_truecolor_black_black() -> None:
    result = _sgr_truecolor((0, 0, 0), (0, 0, 0))
    assert result == "\x1b[38;2;0;0;0;48;2;0;0;0m"


def test_sgr_truecolor_red_fg_blue_bg() -> None:
    result = _sgr_truecolor((255, 0, 0), (0, 0, 255))
    assert result == "\x1b[38;2;255;0;0;48;2;0;0;255m"


# ---------------------------------------------------------------------------
# _nearest_256
# ---------------------------------------------------------------------------


def test_nearest_256_pure_red() -> None:
    # xterm index 196 = pure red (255, 0, 0) in the 216-color cube
    assert _nearest_256(255, 0, 0) == 196


def test_nearest_256_pure_white() -> None:
    # index 15 = bright white (255, 255, 255) in the standard 16
    assert _nearest_256(255, 255, 255) == 15


def test_nearest_256_pure_black() -> None:
    # index 0 = black (0, 0, 0)
    assert _nearest_256(0, 0, 0) == 0


# ---------------------------------------------------------------------------
# _sgr_256
# ---------------------------------------------------------------------------


def test_sgr_256_format() -> None:
    result = _sgr_256((255, 0, 0), (0, 0, 255))
    assert result.startswith("\x1b[38;5;")
    assert ";48;5;" in result
    assert result.endswith("m")


# ---------------------------------------------------------------------------
# _nearest_16
# ---------------------------------------------------------------------------


def test_nearest_16_pure_red() -> None:
    # (255, 0, 0) is closest to dark red (170,0,0) → fg=31, bg=41
    fg, bg = _nearest_16(255, 0, 0)
    assert fg == 31
    assert bg == 41


def test_nearest_16_bright_red() -> None:
    # (255, 85, 85) is bright red → fg=91, bg=101
    fg, bg = _nearest_16(255, 85, 85)
    assert fg == 91
    assert bg == 101


def test_nearest_16_pure_black() -> None:
    fg, bg = _nearest_16(0, 0, 0)
    assert fg == 30
    assert bg == 40


# ---------------------------------------------------------------------------
# _sgr_16
# ---------------------------------------------------------------------------


def test_sgr_16_format() -> None:
    result = _sgr_16((255, 0, 0), (0, 0, 0))
    # Should be \x1b[FG;BGm — two codes separated by ;
    assert result.startswith("\x1b[")
    assert result.endswith("m")
    inner = result[2:-1]
    parts = inner.split(";")
    assert len(parts) == 2
    assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# image_to_ansi_frames — static PNG
# ---------------------------------------------------------------------------


def test_static_png_single_frame() -> None:
    data = _make_png()
    frames, fps = image_to_ansi_frames(data)
    assert len(frames) == 1
    assert fps == 0.0


def test_static_png_starts_with_cursor_home() -> None:
    data = _make_png()
    frames, _ = image_to_ansi_frames(data)
    assert frames[0].startswith("\x1b[H")


def test_static_png_contains_half_block() -> None:
    data = _make_png()
    frames, _ = image_to_ansi_frames(data)
    assert "▄" in frames[0]


def test_static_png_contains_reset() -> None:
    data = _make_png()
    frames, _ = image_to_ansi_frames(data)
    assert "\x1b[0m" in frames[0]


# ---------------------------------------------------------------------------
# All 3 color modes
# ---------------------------------------------------------------------------


def test_truecolor_mode_emits_38_2() -> None:
    data = _make_png(color=(200, 100, 50))
    frames, _ = image_to_ansi_frames(data, cols=4, rows=2, mode="truecolor")
    assert "38;2;" in frames[0]


def test_256_mode_emits_38_5() -> None:
    data = _make_png(color=(200, 100, 50))
    frames, _ = image_to_ansi_frames(data, cols=4, rows=2, mode="256")
    assert "38;5;" in frames[0]


def test_16_mode_does_not_emit_38_2_or_38_5() -> None:
    data = _make_png(color=(200, 100, 50))
    frames, _ = image_to_ansi_frames(data, cols=4, rows=2, mode="16")
    assert "38;2;" not in frames[0]
    assert "38;5;" not in frames[0]


# ---------------------------------------------------------------------------
# Resize
# ---------------------------------------------------------------------------


def test_resize_100x100_to_10x5() -> None:
    data = _make_png(width=100, height=100, color=(0, 200, 0))
    frames, _ = image_to_ansi_frames(data, cols=10, rows=5)
    assert len(frames) == 1
    # 5 rows of output: each ends with \x1b[0m\r\n
    assert frames[0].count("\x1b[0m\r\n") == 5


# ---------------------------------------------------------------------------
# Alpha / transparency
# ---------------------------------------------------------------------------


def test_transparent_png_blends_to_black() -> None:
    data = _make_transparent_png()
    frames, _ = image_to_ansi_frames(data, cols=4, rows=2, mode="truecolor")
    # Transparent pixels → (0,0,0); truecolor SGR should contain 0;0;0
    assert "0;0;0" in frames[0]


# ---------------------------------------------------------------------------
# Animated GIF
# ---------------------------------------------------------------------------


def test_animated_gif_frame_count() -> None:
    data = _make_animated_gif(n_frames=3)
    frames, fps = image_to_ansi_frames(data)
    assert len(frames) == 3


def test_animated_gif_fps_approx_10() -> None:
    data = _make_animated_gif(n_frames=3)
    _, fps = image_to_ansi_frames(data)
    assert abs(fps - 10.0) < 0.1


def test_animated_gif_each_frame_starts_cursor_home() -> None:
    data = _make_animated_gif(n_frames=3)
    frames, _ = image_to_ansi_frames(data)
    for frame in frames:
        assert frame.startswith("\x1b[H")


# ---------------------------------------------------------------------------
# Invalid bytes
# ---------------------------------------------------------------------------


def test_invalid_bytes_raises() -> None:
    from PIL import UnidentifiedImageError

    with pytest.raises(UnidentifiedImageError):
        image_to_ansi_frames(b"this is not an image")


# ---------------------------------------------------------------------------
# Pillow ImportError path
# ---------------------------------------------------------------------------


def test_pillow_import_error_raises_helpful_message() -> None:
    """Cover _render.py:224-225 — ImportError when PIL is not installed."""
    import provide.terminal.shell._render as render_mod

    # Block PIL so the lazy import inside image_to_ansi_frames raises ImportError.
    pil_modules = {k: v for k, v in sys.modules.items() if k == "PIL" or k.startswith("PIL.")}
    for key in pil_modules:
        sys.modules[key] = None  # type: ignore[assignment]
    try:
        importlib.reload(render_mod)
        with pytest.raises(ImportError, match="Pillow") as exc_info:
            render_mod.image_to_ansi_frames(b"data")
        # mutmut_6 prepends 'XX' to the error message; verify message starts with 'missing'
        assert str(exc_info.value).startswith("missing dependency")
    finally:
        # Restore original PIL modules
        for key in pil_modules:
            if pil_modules[key] is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = pil_modules[key]
        importlib.reload(render_mod)


# ---------------------------------------------------------------------------
# _color_dist_sq — all three terms must contribute independently
# ---------------------------------------------------------------------------


def test_color_dist_sq_red_channel() -> None:
    # Only red differs: 10^2 = 100
    assert _color_dist_sq(10, 0, 0, 0, 0, 0) == 100


def test_color_dist_sq_green_channel() -> None:
    # Only green differs: 10^2 = 100
    assert _color_dist_sq(0, 10, 0, 0, 0, 0) == 100


def test_color_dist_sq_blue_channel() -> None:
    # Only blue differs: 10^2 = 100 (catches +/- mutation on b term)
    assert _color_dist_sq(0, 0, 10, 0, 0, 0) == 100


def test_color_dist_sq_all_channels() -> None:
    # 3^2 + 4^2 + 5^2 = 9 + 16 + 25 = 50
    assert _color_dist_sq(3, 4, 5, 0, 0, 0) == 50


def test_color_dist_sq_exponent_is_2() -> None:
    # r diff=3 → 9, not 27 (catches **3 mutation)
    assert _color_dist_sq(3, 0, 0, 0, 0, 0) == 9


def test_color_dist_sq_symmetric() -> None:
    # d(a,b) == d(b,a)
    assert _color_dist_sq(10, 20, 30, 5, 10, 15) == _color_dist_sq(5, 10, 15, 10, 20, 30)


# ---------------------------------------------------------------------------
# _build_xterm256 — verify palette construction (kills all 40 build mutants)
#
# IMPORTANT: _XTERM256 is a module-level mutable list with an early-return guard.
# Tests must clear it and rebuild to exercise the mutation code paths.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def fresh_xterm256() -> Generator[None, None, None]:
    """Clear and rebuild _XTERM256 for each test that needs fresh state."""
    import provide.terminal.shell._render as render_mod

    original = list(render_mod._XTERM256)
    render_mod._XTERM256.clear()
    render_mod._build_xterm256()
    yield
    # Restore original state (may be empty if module was never initialized before)
    render_mod._XTERM256.clear()
    render_mod._XTERM256.extend(original)


def _fresh_build() -> object:
    """Clear _XTERM256 and rebuild via the mutated module, returning the module.

    IMPORTANT: always check assertions against ``rm._XTERM256`` (the module's
    list), NOT the top-level ``_XTERM256`` name imported at module load time.
    When mutmut forks children the parent's sys.modules is inherited; the
    module-level ``_XTERM256`` binding refers to the *parent's* list object,
    which was already populated before the fork.  The mutant only writes to the
    module's own list, so assertions must follow that same reference.
    """
    import provide.terminal.shell._render as render_mod

    render_mod._XTERM256.clear()
    render_mod._build_xterm256()
    return render_mod


def test_build_xterm256_length() -> None:
    rm = _fresh_build()
    assert len(rm._XTERM256) == 256


def test_build_xterm256_first_16_match_ansi16() -> None:
    rm = _fresh_build()
    for idx, (r, g, b, _fg, _bg) in enumerate(rm._ANSI16):
        assert rm._XTERM256[idx] == (r, g, b), f"index {idx} mismatch"


def test_build_xterm256_index_16_is_000() -> None:
    # First cube entry (ri=gi=bi=0) must be pure black (0,0,0)
    rm = _fresh_build()
    assert rm._XTERM256[16] == (0, 0, 0)


def test_build_xterm256_index_17_is_00_95() -> None:
    # ri=0, gi=0, bi=1 → r=0, g=0, b=55+40*1=95
    rm = _fresh_build()
    assert rm._XTERM256[17] == (0, 0, 95)


def test_build_xterm256_index_231_is_255_255_255() -> None:
    # Last cube entry (ri=gi=bi=5) → 55+40*5=255
    rm = _fresh_build()
    assert rm._XTERM256[231] == (255, 255, 255)


def test_build_xterm256_cube_row_r_nonzero() -> None:
    # ri=1 → r = 55 + 40*1 = 95; entry at index 16 + 1*36 = 52 (ri=1,gi=0,bi=0)
    rm = _fresh_build()
    assert rm._XTERM256[52] == (95, 0, 0)


def test_build_xterm256_cube_row_g_nonzero() -> None:
    # ri=0, gi=1, bi=0 → g = 55+40=95; index 16 + 0*36 + 1*6 + 0 = 22
    rm = _fresh_build()
    assert rm._XTERM256[22] == (0, 95, 0)


def test_build_xterm256_cube_row_b_nonzero() -> None:
    # ri=0, gi=0, bi=1 → b = 95; index 17
    rm = _fresh_build()
    assert rm._XTERM256[17][2] == 95


def test_build_xterm256_grayscale_first() -> None:
    # Index 232: v = 8 + 10*0 = 8
    rm = _fresh_build()
    assert rm._XTERM256[232] == (8, 8, 8)


def test_build_xterm256_grayscale_last() -> None:
    # Index 255: v = 8 + 10*23 = 238
    rm = _fresh_build()
    assert rm._XTERM256[255] == (238, 238, 238)


def test_build_xterm256_grayscale_mid() -> None:
    # Index 244 = 232 + 12: v = 8 + 10*12 = 128
    rm = _fresh_build()
    assert rm._XTERM256[244] == (128, 128, 128)


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
    from provide.terminal.shell._render import _render_frame
    from provide.terminal.shell._render import _sgr_truecolor as sgr_fn

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


def test_render_frame_y_plus_1_not_y_minus_1() -> None:
    # mutmut_19 changes y+1 to y-1; at y=2 in a 4-row image:
    #   correct:  pixels[x, 3] (row 3 = yellow, fg=yellow, bg=blue)
    #   mutant:   pixels[x, 1] (row 1 = green, fg=green, bg=blue)
    # Verify that the combined SGR for row 1 (y=2) uses yellow fg + blue bg,
    # not green fg + blue bg.
    from provide.terminal.shell._render import _render_frame
    from provide.terminal.shell._render import _sgr_truecolor as sgr_fn

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
