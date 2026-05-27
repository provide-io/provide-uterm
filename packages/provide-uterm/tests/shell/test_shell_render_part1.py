#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.shell._render — ANSI image rendering."""

from __future__ import annotations

import importlib
import io
import sys
from collections.abc import Generator

import pytest
from PIL import Image

from provide.uterm.shell._render import (
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
    import provide.uterm.shell._render as render_mod

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
    import provide.uterm.shell._render as render_mod

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
    import provide.uterm.shell._render as render_mod

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
