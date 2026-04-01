#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.shell._render — ANSI image rendering."""

from __future__ import annotations

import importlib
import io
import sys

import pytest
from PIL import Image

from provide.shell._render import (
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
    import provide.shell._render as render_mod

    # Block PIL so the lazy import inside image_to_ansi_frames raises ImportError.
    pil_modules = {k: v for k, v in sys.modules.items() if k == "PIL" or k.startswith("PIL.")}
    for key in pil_modules:
        sys.modules[key] = None  # type: ignore[assignment]
    try:
        importlib.reload(render_mod)
        with pytest.raises(ImportError, match="Pillow"):
            render_mod.image_to_ansi_frames(b"data")
    finally:
        # Restore original PIL modules
        for key in pil_modules:
            if pil_modules[key] is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = pil_modules[key]
        importlib.reload(render_mod)
