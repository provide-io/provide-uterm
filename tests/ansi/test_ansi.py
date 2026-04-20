#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.ansi."""

from __future__ import annotations

from provide.terminal.ansi import BOLD, CLEAR_SCREEN, DEFAULT_RGB, RESET, _color256_to_rgb, colorize, strip_colors


class TestColorize:
    def test_bright_cyan(self) -> None:
        result = colorize("{+c}hello{-x}")
        assert result == "\033[1;36mhello\033[0m"

    def test_multiple_tags(self) -> None:
        result = colorize("{+r}red{-x} {+g}green{-x}")
        assert "\033[1;31m" in result
        assert "\033[1;32m" in result
        assert "\033[0m" in result

    def test_unknown_tag_passthrough(self) -> None:
        result = colorize("{+z}unknown{-x}")
        assert "{+z}" in result

    def test_no_tags(self) -> None:
        text = "plain text"
        assert colorize(text) == text

    def test_bold_white(self) -> None:
        result = colorize("{+Bw}header{-x}")
        assert "\033[1;37m" in result

    def test_empty_string(self) -> None:
        assert colorize("") == ""


class TestStripColors:
    def test_removes_tag(self) -> None:
        result = strip_colors("{+c}hello{-x}")
        assert result == "hello"

    def test_removes_all_tags(self) -> None:
        result = strip_colors("{+r}red{-x} {+g}green{-x}")
        assert result == "red green"

class TestDialectRegistry:
    def test_builtins_registered(self) -> None:
        assert registered_dialects() == ["brace_tokens", "extended_tokens", "tilde_codes", "pipe_codes"]

    def test_empty_string(self) -> None:
        assert strip_colors("") == ""

    def test_round_trip_plain(self) -> None:
        """strip_colors(colorize(text)) should equal strip_colors(text) for plain text."""
        text = "no tags here"
        assert strip_colors(colorize(text)) == strip_colors(text)


class TestColor256ToRgb:
    """Tests for _color256_to_rgb boundary conditions.

    Kills mutmut_1: `if idx < 16` → `if idx <= 16`.
    With that mutation, idx=16 hits DEFAULT_RGB[16] → IndexError (list has only 16 entries).
    """

    def test_index_15_uses_default_table(self) -> None:
        """Last system colour (index 15) must come from DEFAULT_RGB."""
        assert _color256_to_rgb(15) == DEFAULT_RGB[15]

    def test_index_16_is_first_256_cube_colour(self) -> None:
        """Index 16 is the first entry of the 6x6x6 colour cube (not in DEFAULT_RGB).

        With `idx <= 16`, DEFAULT_RGB[16] raises IndexError.
        With `idx < 16` (correct), idx 16 → cube → (0, 0, 0) black.
        """
        result = _color256_to_rgb(16)
        # idx=16: subtract 16 → 0; b=0, g=0, r=0; levels[0]=0 → (0,0,0)
        assert result == (0, 0, 0), f"Expected (0,0,0) for index 16, got {result}"

    def test_index_0_is_black(self) -> None:
        assert _color256_to_rgb(0) == DEFAULT_RGB[0]

    def test_index_231_is_last_cube_colour(self) -> None:
        """Index 231 is the last entry of the 6x6x6 cube."""
        r, g, b = _color256_to_rgb(231)
        assert (r, g, b) == (255, 255, 255)

    def test_index_232_is_first_greyscale(self) -> None:
        """Index 232 is the first greyscale ramp entry: gray = 8."""
        assert _color256_to_rgb(232) == (8, 8, 8)

    def test_index_255_is_last_greyscale(self) -> None:
        """Index 255 is the last greyscale entry: gray = 8 + 23*10 = 238."""
        assert _color256_to_rgb(255) == (238, 238, 238)


class TestConstants:
    def test_clear_screen_has_escape(self) -> None:
        assert CLEAR_SCREEN.startswith("\033[")

    def test_bold_has_escape(self) -> None:
        assert BOLD.startswith("\033[")

    def test_reset_has_escape(self) -> None:
        assert RESET == "\033[0m"
