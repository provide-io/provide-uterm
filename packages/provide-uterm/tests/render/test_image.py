#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.render.image — image-to-ANSI conversion."""

from __future__ import annotations

import importlib
import io
import sys

import pytest
from PIL import Image

from provide.uterm.render.image import image_to_ansi_frames, render_frame
from provide.uterm.render.sgr import sgr_truecolor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png(width: int = 4, height: int = 4, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_transparent_png(width: int = 4, height: int = 4) -> bytes:
    img = Image.new("RGBA", (width, height), (255, 0, 0, 0))
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


def _make_rgba_png(pixels_rgba: list[list[tuple[int, int, int, int]]]) -> bytes:
    h = len(pixels_rgba)
    w = len(pixels_rgba[0])
    img = Image.new("RGBA", (w, h))
    img.putdata([px for row in pixels_rgba for px in row])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_gif_with_duration(duration_ms: int, n_frames: int = 2) -> bytes:
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


# ---------------------------------------------------------------------------
# Static PNG
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
    assert "\u2584" in frames[0]


def test_static_png_contains_reset() -> None:
    data = _make_png()
    frames, _ = image_to_ansi_frames(data)
    assert "\x1b[0m" in frames[0]


# ---------------------------------------------------------------------------
# Color modes
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
    assert frames[0].count("\x1b[0m\r\n") == 5


# ---------------------------------------------------------------------------
# Transparency
# ---------------------------------------------------------------------------


def test_transparent_png_blends_to_black() -> None:
    data = _make_transparent_png()
    frames, _ = image_to_ansi_frames(data, cols=4, rows=2, mode="truecolor")
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
    import provide.uterm.render.image as image_mod

    pil_modules = {k: v for k, v in sys.modules.items() if k == "PIL" or k.startswith("PIL.")}
    for key in pil_modules:
        sys.modules[key] = None  # type: ignore[assignment]
    try:
        importlib.reload(image_mod)
        with pytest.raises(ImportError, match="Pillow") as exc_info:
            image_mod.image_to_ansi_frames(b"data")
        assert str(exc_info.value).startswith("missing dependency")
    finally:
        for key in pil_modules:
            if pil_modules[key] is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = pil_modules[key]
        importlib.reload(image_mod)


# ---------------------------------------------------------------------------
# render_frame — detailed pixel-level checks
# ---------------------------------------------------------------------------


def test_render_frame_cursor_home_present() -> None:
    data = _make_png(2, 2, (255, 0, 0))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    assert frames[0][0:3] == "\x1b[H"


def test_render_frame_uses_bottom_pixel_as_fg() -> None:
    pixels = [
        [(0, 0, 255, 255)],
        [(255, 0, 0, 255)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "38;2;255;0;0" in frames[0]
    assert "48;2;0;0;255" in frames[0]


def test_render_frame_fg_bg_not_swapped() -> None:
    pixels = [
        [(0, 255, 0, 255)],
        [(0, 0, 255, 255)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "38;2;0;0;255" in frames[0]
    assert "48;2;0;255;0" in frames[0]


def test_render_frame_transparent_top_pixel_zeroed() -> None:
    pixels = [
        [(255, 0, 0, 0)],
        [(0, 0, 255, 255)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "48;2;0;0;0" in frames[0]


def test_render_frame_transparent_bottom_pixel_zeroed() -> None:
    pixels = [
        [(0, 255, 0, 255)],
        [(255, 0, 0, 0)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "38;2;0;0;0" in frames[0]


def test_render_frame_alpha_threshold_127_transparent() -> None:
    pixels = [
        [(200, 100, 50, 127)],
        [(0, 0, 0, 255)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "48;2;0;0;0" in frames[0]


def test_render_frame_alpha_threshold_128_opaque() -> None:
    pixels = [
        [(200, 100, 50, 128)],
        [(0, 0, 0, 255)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "48;2;200;100;50" in frames[0]


def test_render_frame_odd_height_last_row_padding() -> None:
    class FakePixels:
        def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int, int]:
            x, y = xy
            return (0, 255, 0, 255)

    result = render_frame(FakePixels(), 1, 1, sgr_truecolor)
    assert "38;2;0;0;0" in result
    assert "48;2;0;255;0" in result


def test_render_frame_sgr_deduplication() -> None:
    data = _make_png(2, 2, (255, 0, 0))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    frame = frames[0]
    after_home = frame[3:]
    assert after_home.count("\x1b[38;2;") == 1


def test_render_frame_half_block_count_matches_pixels() -> None:
    data = _make_png(4, 2, (0, 128, 0))
    frames, _ = image_to_ansi_frames(data, cols=4, rows=1, mode="truecolor")
    assert frames[0].count("\u2584") == 4


def test_render_frame_row_ends_with_reset_and_crlf() -> None:
    data = _make_png(2, 2, (0, 0, 128))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    assert "\x1b[0m\r\n" in frames[0]


def test_render_frame_return_string_joined() -> None:
    data = _make_png(2, 4, (128, 0, 128))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=2, mode="truecolor")
    assert isinstance(frames[0], str)


def test_render_frame_bottom_pixel_alpha_128_is_opaque() -> None:
    pixels = [
        [(0, 255, 0, 255)],
        [(0, 0, 200, 128)],
    ]
    data = _make_rgba_png(pixels)
    frames, _ = image_to_ansi_frames(data, cols=1, rows=1, mode="truecolor")
    assert "38;2;0;0;200" in frames[0]


def test_render_frame_y_plus_1_not_y_minus_1() -> None:
    row_colors = [
        (255, 0, 0, 255),
        (0, 255, 0, 255),
        (0, 0, 255, 255),
        (255, 255, 0, 255),
    ]

    class FakePixels4:
        def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int, int]:
            x, y = xy
            return row_colors[y % len(row_colors)]

    result = render_frame(FakePixels4(), 1, 4, sgr_truecolor)
    assert "38;2;255;255;0;48;2;0;0;255" in result
    assert "38;2;0;255;0;48;2;0;0;255" not in result


def test_render_frame_row_ends_exactly_with_reset_crlf() -> None:
    data = _make_png(2, 2, (0, 0, 128))
    frames, _ = image_to_ansi_frames(data, cols=2, rows=1, mode="truecolor")
    assert frames[0].endswith("\x1b[0m\r\n")


# ---------------------------------------------------------------------------
# image_to_ansi_frames — defaults and edge cases
# ---------------------------------------------------------------------------


def test_default_cols_80() -> None:
    data = _make_png(80, 48, (0, 200, 0))
    frames, _ = image_to_ansi_frames(data)
    assert frames[0].count("\u2584") == 80 * 24


def test_default_rows_24() -> None:
    data = _make_png(4, 4, (0, 0, 200))
    frames, _ = image_to_ansi_frames(data, cols=4)
    assert frames[0].count("\x1b[0m\r\n") == 24


def test_static_image_n_frames_fallback_to_1() -> None:
    data = _make_png()
    frames, fps = image_to_ansi_frames(data)
    assert len(frames) == 1
    assert fps == 0.0


def test_duration_zero_gives_fps_zero() -> None:
    data = _make_png()
    _, fps = image_to_ansi_frames(data)
    assert fps == 0.0


def test_fps_calculation_100ms_duration() -> None:
    data = _make_animated_gif(n_frames=2)
    _, fps = image_to_ansi_frames(data)
    assert abs(fps - 10.0) < 0.01


def test_fps_calculation_exact_value() -> None:
    data = _make_animated_gif(n_frames=2)
    _, fps = image_to_ansi_frames(data)
    assert fps == pytest.approx(10.0)


def test_fps_calculation_10ms_duration() -> None:
    data = _make_gif_with_duration(10, n_frames=2)
    _, fps = image_to_ansi_frames(data)
    assert fps == pytest.approx(100.0)


def test_lanczos_resize_produces_correct_dimensions() -> None:
    data = _make_png(200, 200, (100, 150, 200))
    frames, _ = image_to_ansi_frames(data, cols=10, rows=5)
    assert frames[0].count("\x1b[0m\r\n") == 5
    assert frames[0].count("\u2584") == 10 * 5


def test_lanczos_produces_expected_pixel_values() -> None:
    img = Image.new("RGBA", (100, 100))
    pix = img.load()
    for yy in range(100):
        for xx in range(100):
            pix[xx, yy] = (xx * 2 % 256, yy * 2 % 256, (xx + yy) % 256, 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    expected_img = Image.open(io.BytesIO(data)).convert("RGBA").resize((4, 2), Image.LANCZOS)
    expected_bg = expected_img.getpixel((0, 0))[:3]

    frames, _ = image_to_ansi_frames(data, cols=4, rows=1, mode="truecolor")
    expected_bg_sgr = f"48;2;{expected_bg[0]};{expected_bg[1]};{expected_bg[2]}"
    assert expected_bg_sgr in frames[0]

    bicubic_img = Image.open(io.BytesIO(data)).convert("RGBA").resize((4, 2), None)
    bicubic_bg = bicubic_img.getpixel((0, 0))[:3]
    assert expected_bg != bicubic_bg
