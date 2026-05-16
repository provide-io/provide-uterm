#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for render leaf modules after removing the ansi_render shim."""

from __future__ import annotations

from provide.uterm.render.buffer import (
    ANSI_RESET,
    BG_CODES,
    FG_CODES,
    AnsiBuffer,
    clear_screen,
    move_to,
    style_to_sgr,
)


class TestAnsiBuffer:
    """AnsiBuffer construction, feed, and render."""

    def test_construction(self) -> None:
        buf = AnsiBuffer(80, 25)
        assert buf._screen.columns == 80
        assert buf._screen.lines == 25

    def test_feed_empty_data(self) -> None:
        buf = AnsiBuffer(80, 25)
        buf.feed(b"")  # should not raise

    def test_feed_and_render_plain_text(self) -> None:
        buf = AnsiBuffer(40, 5)
        buf.feed(b"Hello")
        lines = buf.render_lines(40, 5)
        assert len(lines) == 5
        # First line should contain "Hello" somewhere
        assert "Hello" in lines[0]

    def test_render_lines_contain_ansi_codes(self) -> None:
        buf = AnsiBuffer(40, 5)
        # Feed bold red text via ANSI escape
        buf.feed(b"\x1b[1;31mRED\x1b[0m")
        lines = buf.render_lines(40, 5)
        first_line = lines[0]
        # Should contain SGR sequence for bold (1) + red fg (31)
        assert "\x1b[1;31m" in first_line
        assert "RED" in first_line

    def test_resize(self) -> None:
        buf = AnsiBuffer(80, 25)
        buf.resize(120, 40)
        # pyte.Screen.resize takes (lines, columns) internally;
        # AnsiBuffer.resize passes (cols, rows) so cols→columns, rows→lines
        assert buf._screen.columns == 120
        assert buf._screen.lines == 40

    def test_reset(self) -> None:
        buf = AnsiBuffer(40, 5)
        buf.feed(b"Some text")
        buf.reset()
        lines = buf.render_lines(40, 5)
        # After reset all lines should be blank (spaces + reset)
        for line in lines:
            stripped = line.replace(ANSI_RESET, "").replace("\x1b[0m", "").strip()
            assert stripped == ""


class TestStyleToSgr:
    """style_to_sgr for various attribute combinations."""

    def test_default_returns_reset(self) -> None:
        result = style_to_sgr("default", "default", False, False, False, False)
        assert result == ANSI_RESET

    def test_bold(self) -> None:
        result = style_to_sgr("default", "default", True, False, False, False)
        assert result == "\x1b[1m"

    def test_fg_color(self) -> None:
        result = style_to_sgr("green", "default", False, False, False, False)
        assert result == f"\x1b[{FG_CODES['green']}m"

    def test_bg_color(self) -> None:
        result = style_to_sgr("default", "blue", False, False, False, False)
        assert result == f"\x1b[{BG_CODES['blue']}m"

    def test_bold_and_color(self) -> None:
        result = style_to_sgr("red", "default", True, False, False, False)
        assert result == f"\x1b[1;{FG_CODES['red']}m"

    def test_reverse_swaps_fg_bg(self) -> None:
        # reverse=True should swap fg and bg before lookup
        result = style_to_sgr("red", "blue", False, False, True, False)
        # After swap: fg=blue, bg=red
        assert f"{FG_CODES['blue']}" in result
        assert f"{BG_CODES['red']}" in result

    def test_underscore(self) -> None:
        result = style_to_sgr("default", "default", False, True, False, False)
        assert result == "\x1b[4m"

    def test_blink(self) -> None:
        result = style_to_sgr("default", "default", False, False, False, True)
        assert result == "\x1b[5m"


class TestTruecolorSupport:
    """256-color and truecolor (hex) handling in style_to_sgr."""

    def test_hex_fg_truecolor(self) -> None:
        """Hex fg like 'ff8000' emits 38;2;R;G;B."""
        result = style_to_sgr("ff8000", "default", False, False, False, False)
        assert result == "\x1b[38;2;255;128;0m"

    def test_hex_bg_truecolor(self) -> None:
        """Hex bg like '0000ff' emits 48;2;R;G;B."""
        result = style_to_sgr("default", "0000ff", False, False, False, False)
        assert result == "\x1b[48;2;0;0;255m"

    def test_hex_fg_red(self) -> None:
        """256-color red (ff0000) as truecolor."""
        result = style_to_sgr("ff0000", "default", False, False, False, False)
        assert result == "\x1b[38;2;255;0;0m"

    def test_hex_fg_and_bg(self) -> None:
        """Both fg and bg as hex."""
        result = style_to_sgr("00ff00", "800080", False, False, False, False)
        assert "38;2;0;255;0" in result
        assert "48;2;128;0;128" in result

    def test_hex_with_bold(self) -> None:
        """Hex color combined with bold."""
        result = style_to_sgr("ff8000", "default", True, False, False, False)
        assert "1;" in result
        assert "38;2;255;128;0" in result

    def test_named_fg_hex_bg(self) -> None:
        """Mix named fg + hex bg."""
        result = style_to_sgr("red", "0000ff", False, False, False, False)
        assert f"{FG_CODES['red']}" in result
        assert "48;2;0;0;255" in result

    def test_hex_with_reverse(self) -> None:
        """Reverse swaps hex fg and bg."""
        result = style_to_sgr("ff0000", "0000ff", False, False, True, False)
        # After swap: fg=0000ff, bg=ff0000
        assert "38;2;0;0;255" in result
        assert "48;2;255;0;0" in result

    def test_invalid_hex_too_short(self) -> None:
        """Non-6-char string treated as default (no color codes)."""
        result = style_to_sgr("fff", "default", False, False, False, False)
        assert result == ANSI_RESET

    def test_invalid_hex_too_long(self) -> None:
        result = style_to_sgr("ff00ff00", "default", False, False, False, False)
        assert result == ANSI_RESET

    def test_invalid_hex_non_hex_chars(self) -> None:
        result = style_to_sgr("gghhii", "default", False, False, False, False)
        assert result == ANSI_RESET

    def test_black_hex(self) -> None:
        """000000 should emit truecolor black, not be confused with 'default'."""
        result = style_to_sgr("000000", "default", False, False, False, False)
        assert result == "\x1b[38;2;0;0;0m"

    def test_white_hex(self) -> None:
        result = style_to_sgr("ffffff", "default", False, False, False, False)
        assert result == "\x1b[38;2;255;255;255m"


class TestAnsiBufferTruecolor:
    """Full AnsiBuffer roundtrip with 256-color and truecolor input."""

    def test_256_color_roundtrip(self) -> None:
        """Feed 256-color ESC[38;5;196m → pyte hex → render with truecolor SGR."""
        buf = AnsiBuffer(40, 5)
        buf.feed(b"\x1b[38;5;196mRED256\x1b[0m")
        lines = buf.render_lines(40, 5)
        first = lines[0]
        assert "RED256" in first
        # pyte converts 256-color to hex; render should emit 38;2;R;G;B
        assert "38;2;" in first

    def test_truecolor_roundtrip(self) -> None:
        """Feed truecolor ESC[38;2;255;128;0m → pyte hex → render with truecolor SGR."""
        buf = AnsiBuffer(40, 5)
        buf.feed(b"\x1b[38;2;255;128;0mORANGE\x1b[0m")
        lines = buf.render_lines(40, 5)
        first = lines[0]
        assert "ORANGE" in first
        assert "38;2;255;128;0" in first

    def test_truecolor_bg_roundtrip(self) -> None:
        """Truecolor background preserved through render."""
        buf = AnsiBuffer(40, 5)
        buf.feed(b"\x1b[48;2;0;0;128mBLUEBG\x1b[0m")
        lines = buf.render_lines(40, 5)
        first = lines[0]
        assert "BLUEBG" in first
        assert "48;2;" in first

    def test_mixed_named_and_truecolor(self) -> None:
        """Named bold + truecolor fg in same buffer."""
        buf = AnsiBuffer(40, 5)
        buf.feed(b"\x1b[1;31mBOLD\x1b[0m \x1b[38;2;100;200;50mTRUE\x1b[0m")
        lines = buf.render_lines(40, 5)
        first = lines[0]
        assert "BOLD" in first
        assert "TRUE" in first


class TestHelperFunctions:
    """Test _is_hex_color, _hex_to_rgb, _color_sgr."""

    def test_is_hex_color_valid(self) -> None:
        from provide.uterm.render.buffer import _is_hex_color

        assert _is_hex_color("ff8000") is True
        assert _is_hex_color("000000") is True
        assert _is_hex_color("FFFFFF") is True

    def test_is_hex_color_invalid(self) -> None:
        from provide.uterm.render.buffer import _is_hex_color

        assert _is_hex_color("fff") is False
        assert _is_hex_color("gggggg") is False
        assert _is_hex_color("default") is False
        assert _is_hex_color("") is False

    def test_hex_to_rgb(self) -> None:
        from provide.uterm.render.buffer import _hex_to_rgb

        assert _hex_to_rgb("ff8000") == (255, 128, 0)
        assert _hex_to_rgb("000000") == (0, 0, 0)
        assert _hex_to_rgb("ffffff") == (255, 255, 255)

    def test_color_sgr_default(self) -> None:
        from provide.uterm.render.buffer import _color_sgr

        assert _color_sgr("default", is_fg=True) == []
        assert _color_sgr("default", is_fg=False) == []

    def test_color_sgr_named(self) -> None:
        from provide.uterm.render.buffer import _color_sgr

        assert _color_sgr("red", is_fg=True) == [31]
        assert _color_sgr("blue", is_fg=False) == [44]

    def test_color_sgr_hex(self) -> None:
        from provide.uterm.render.buffer import _color_sgr

        assert _color_sgr("ff8000", is_fg=True) == [38, 2, 255, 128, 0]
        assert _color_sgr("0000ff", is_fg=False) == [48, 2, 0, 0, 255]

    def test_color_sgr_unknown(self) -> None:
        from provide.uterm.render.buffer import _color_sgr

        assert _color_sgr("notacolor", is_fg=True) == []


class TestEscapeHelpers:
    """move_to and clear_screen return correct escape sequences."""

    def test_move_to(self) -> None:
        assert move_to(1, 1) == "\x1b[1;1H"
        assert move_to(10, 20) == "\x1b[10;20H"

    def test_clear_screen(self) -> None:
        assert clear_screen() == "\x1b[2J"
