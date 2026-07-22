#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.shell._commands — render command."""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from provide.uterm.shell.commands import AnimatedResult, CommandDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_dispatcher(ctx: dict[str, Any] | None = None) -> CommandDispatcher:
    return CommandDispatcher(ctx or {})


def first_data(frames: list[str]) -> str:
    return frames[0]


def _make_png_bytes(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    img = Image.new("RGB", (4, 4), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_gif_bytes(n_frames: int = 3) -> bytes:
    frames = [Image.new("RGB", (4, 4), (i * 80, 255, 0)) for i in range(n_frames)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


def _mock_urlopen(data: bytes, status: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.status = status
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_render_no_args():
    d = make_dispatcher()
    frames = await d.dispatch("render")
    assert isinstance(frames, list)
    assert "usage:" in first_data(frames)


async def test_render_in_help():
    d = make_dispatcher()
    frames = await d.dispatch("help")
    assert isinstance(frames, list)
    assert "render" in first_data(frames)


async def test_render_help_detail():
    d = make_dispatcher()
    frames = await d.dispatch("help render")
    assert isinstance(frames, list)
    assert "--mode" in first_data(frames)


async def test_render_static_png():
    d = make_dispatcher()
    png_bytes = _make_png_bytes()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(png_bytes)):
        result = await d.dispatch("render https://example.com/image.png")
    assert isinstance(result, list)
    assert "▄" in first_data(result)


async def test_render_mode_256():
    d = make_dispatcher()
    png_bytes = _make_png_bytes()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(png_bytes)):
        result = await d.dispatch("render --mode 256 https://example.com/image.png")
    assert isinstance(result, list)
    assert "38;5;" in first_data(result)


async def test_render_mode_16():
    d = make_dispatcher()
    png_bytes = _make_png_bytes()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(png_bytes)):
        result = await d.dispatch("render --mode 16 https://example.com/image.png")
    assert isinstance(result, list)
    output = first_data(result)
    assert "38;2;" not in output
    assert "38;5;" not in output


async def test_render_custom_cols_rows():
    d = make_dispatcher()
    png_bytes = _make_png_bytes()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(png_bytes)):
        result = await d.dispatch("render --cols 10 --rows 5 https://example.com/image.png")
    assert isinstance(result, list)
    assert "▄" in first_data(result)


async def test_render_animated_gif_returns_animated_result():
    d = make_dispatcher()
    gif_bytes = _make_gif_bytes(n_frames=3)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(gif_bytes)):
        result = await d.dispatch("render https://example.com/anim.gif")
    assert isinstance(result, AnimatedResult)
    assert len(result.frames) == 3
    assert result.fps > 0
    assert result.loop is False


async def test_render_loop_flag():
    d = make_dispatcher()
    gif_bytes = _make_gif_bytes(n_frames=3)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(gif_bytes)):
        result = await d.dispatch("render --loop https://example.com/anim.gif")
    assert isinstance(result, AnimatedResult)
    assert result.loop is True


async def test_render_fps_override():
    d = make_dispatcher()
    gif_bytes = _make_gif_bytes(n_frames=3)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(gif_bytes)):
        result = await d.dispatch("render --fps 5 https://example.com/anim.gif")
    assert isinstance(result, AnimatedResult)
    assert result.fps == pytest.approx(5.0)


async def test_render_file_url(tmp_path):
    d = make_dispatcher()
    png_bytes = _make_png_bytes()
    img_file = tmp_path / "test.png"
    img_file.write_bytes(png_bytes)
    result = await d.dispatch(f"render file://{img_file}")
    assert isinstance(result, list)
    assert "▄" in first_data(result)


async def test_render_file_url_with_space(tmp_path):
    # A path containing whitespace must arrive percent-encoded (render's arg is
    # whitespace-tokenized); the file:// branch percent-decodes it back.
    d = make_dispatcher()
    sub = tmp_path / "My Demos"
    sub.mkdir()
    img_file = sub / "test.png"
    img_file.write_bytes(_make_png_bytes())
    result = await d.dispatch(f"render {img_file.as_uri()}")
    assert isinstance(result, list)
    assert "▄" in first_data(result)


async def test_render_bad_url_scheme():
    d = make_dispatcher()
    result = await d.dispatch("render ftp://example.com/image.png")
    assert isinstance(result, list)
    assert "unsupported URL scheme" in first_data(result)


async def test_render_network_error():
    d = make_dispatcher()
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        result = await d.dispatch("render https://example.com/image.png")
    assert isinstance(result, list)
    assert "error:" in first_data(result).lower() or "cannot fetch" in first_data(result)


async def test_render_invalid_image():
    d = make_dispatcher()
    bad_bytes = b"not an image at all"
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(bad_bytes)):
        result = await d.dispatch("render https://example.com/bad.png")
    assert isinstance(result, list)
    assert "error:" in first_data(result).lower() or "cannot decode" in first_data(result)


async def test_render_missing_pillow():
    d = make_dispatcher()
    png_bytes = _make_png_bytes()
    with (
        patch("urllib.request.urlopen", return_value=_mock_urlopen(png_bytes)),
        patch(
            "provide.uterm.shell._render.image_to_ansi_frames",
            side_effect=ImportError(
                "missing dependency — Pillow\ninstall the images extra: pip install 'provide-uterm[emulator]'"
            ),
        ),
    ):
        result = await d.dispatch("render https://example.com/image.png")
    assert isinstance(result, list)
    assert "error:" in first_data(result).lower() or "missing dependency" in first_data(result)


async def test_render_unknown_mode():
    d = make_dispatcher()
    result = await d.dispatch("render --mode rainbow https://example.com/image.png")
    assert isinstance(result, list)
    assert "unknown mode" in first_data(result)


async def test_render_file_not_found():
    d = make_dispatcher()
    result = await d.dispatch("render file:///nonexistent/path/image.png")
    assert isinstance(result, list)
    assert "error:" in first_data(result).lower() or "file not found" in first_data(result)


async def test_render_unknown_flag():
    """Cover the unknown flag path (_commands.py:441)."""
    d = make_dispatcher()
    result = await d.dispatch("render --bogus-flag https://example.com/image.png")
    assert isinstance(result, list)
    assert "unknown flag" in first_data(result)


async def test_render_flags_only_no_url():
    """Cover the no-url-after-flags path (_commands.py:444)."""
    d = make_dispatcher()
    result = await d.dispatch("render --loop")
    assert isinstance(result, list)
    assert "usage:" in first_data(result)


async def test_render_empty_image():
    """Cover the empty-image path (_commands.py:480 else branch)."""
    d = make_dispatcher()
    png_bytes = _make_png_bytes()
    with (
        patch("urllib.request.urlopen", return_value=_mock_urlopen(png_bytes)),
        patch(
            "provide.uterm.shell._render.image_to_ansi_frames",
            return_value=([], 0.0),
        ),
    ):
        result = await d.dispatch("render https://example.com/image.png")
    assert isinstance(result, list)
    assert "empty image" in first_data(result)
