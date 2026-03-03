#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for provide.terminal.ansi."""

from __future__ import annotations

from provide.terminal.ansi import BOLD, CLEAR_SCREEN, RESET, colorize, strip_colors


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

    def test_no_tags(self) -> None:
        text = "plain text"
        assert strip_colors(text) == text

    def test_empty_string(self) -> None:
        assert strip_colors("") == ""

    def test_round_trip_plain(self) -> None:
        """strip_colors(colorize(text)) should equal strip_colors(text) for plain text."""
        text = "no tags here"
        assert strip_colors(colorize(text)) == strip_colors(text)


class TestConstants:
    def test_clear_screen_has_escape(self) -> None:
        assert CLEAR_SCREEN.startswith("\033[")

    def test_bold_has_escape(self) -> None:
        assert BOLD.startswith("\033[")

    def test_reset_has_escape(self) -> None:
        assert RESET == "\033[0m"
