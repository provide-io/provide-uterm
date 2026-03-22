#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for provide.terminal.ansi."""

from __future__ import annotations

import pytest

from provide.terminal.ansi import (
    BOLD,
    CLEAR_SCREEN,
    DEFAULT_RGB,
    RESET,
    _color256_to_rgb,
    _handle_pipe_codes,
    normalize_colors,
    register_color_dialect,
    registered_dialects,
    unregister_color_dialect,
)


@pytest.fixture()
def _save_registry():
    """Save and restore dialect registry around a test."""
    from provide.terminal.ansi import _dialect_registry

    saved = list(_dialect_registry)
    yield
    _dialect_registry.clear()
    _dialect_registry.extend(saved)


# ---------------------------------------------------------------------------
# Dialect registry
# ---------------------------------------------------------------------------


class TestDialectRegistry:
    def test_builtins_registered(self) -> None:
        assert registered_dialects() == ["brace_tokens", "extended_tokens", "tilde_codes", "pipe_codes"]

    @pytest.mark.usefixtures("_save_registry")
    def test_register_and_list(self) -> None:
        register_color_dialect("test_dialect", lambda t: t)
        assert "test_dialect" in registered_dialects()

    @pytest.mark.usefixtures("_save_registry")
    def test_register_duplicate_raises(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            register_color_dialect("pipe_codes", lambda t: t)

    @pytest.mark.usefixtures("_save_registry")
    def test_unregister(self) -> None:
        register_color_dialect("temp", lambda t: t)
        assert "temp" in registered_dialects()
        unregister_color_dialect("temp")
        assert "temp" not in registered_dialects()

    def test_unregister_missing_raises(self) -> None:
        with pytest.raises(KeyError, match="not registered"):
            unregister_color_dialect("nonexistent")

    @pytest.mark.usefixtures("_save_registry")
    def test_dialects_called_in_order(self) -> None:
        calls: list[str] = []

        def handler_a(t: str) -> str:
            calls.append("a")
            return t

        def handler_b(t: str) -> str:
            calls.append("b")
            return t

        register_color_dialect("a", handler_a)
        register_color_dialect("b", handler_b)
        normalize_colors("test")
        # Built-ins run first, then a, then b
        assert calls == ["a", "b"]


# ---------------------------------------------------------------------------
# Pipe codes
# ---------------------------------------------------------------------------


class TestPipeCodes:
    def test_pipe_00_black_fg(self) -> None:
        assert _handle_pipe_codes("|00") == "\x1b[30m"

    def test_pipe_01_blue_fg(self) -> None:
        assert _handle_pipe_codes("|01") == "\x1b[34m"

    def test_pipe_02_green_fg(self) -> None:
        assert _handle_pipe_codes("|02") == "\x1b[32m"

    def test_pipe_03_cyan_fg(self) -> None:
        assert _handle_pipe_codes("|03") == "\x1b[36m"

    def test_pipe_04_red_fg(self) -> None:
        assert _handle_pipe_codes("|04") == "\x1b[31m"

    def test_pipe_05_magenta_fg(self) -> None:
        assert _handle_pipe_codes("|05") == "\x1b[35m"

    def test_pipe_06_brown_fg(self) -> None:
        assert _handle_pipe_codes("|06") == "\x1b[33m"

    def test_pipe_07_white_fg(self) -> None:
        assert _handle_pipe_codes("|07") == "\x1b[37m"

    def test_pipe_08_bright_black(self) -> None:
        assert _handle_pipe_codes("|08") == "\x1b[90m"

    def test_pipe_09_bright_blue(self) -> None:
        assert _handle_pipe_codes("|09") == "\x1b[94m"

    def test_pipe_15_bright_white(self) -> None:
        assert _handle_pipe_codes("|15") == "\x1b[97m"

    def test_pipe_16_bg_black(self) -> None:
        assert _handle_pipe_codes("|16") == "\x1b[40m"

    def test_pipe_17_bg_blue(self) -> None:
        assert _handle_pipe_codes("|17") == "\x1b[44m"

    def test_pipe_23_bg_white(self) -> None:
        assert _handle_pipe_codes("|23") == "\x1b[47m"

    def test_pipe_24_passthrough(self) -> None:
        assert _handle_pipe_codes("|24") == "|24"

    def test_pipe_mixed_with_text(self) -> None:
        result = _handle_pipe_codes("|04Red |02Green")
        assert "\x1b[31m" in result
        assert "\x1b[32m" in result
        assert "Red " in result
        assert "Green" in result

    def test_pipe_via_normalize_colors(self) -> None:
        result = normalize_colors("|04Red|00")
        assert "\x1b[31m" in result
        assert "\x1b[30m" in result

    def test_pipe_single_digit_passthrough(self) -> None:
        """A pipe followed by a single digit is not a valid pipe code."""
        assert _handle_pipe_codes("|4 text") == "|4 text"


# ---------------------------------------------------------------------------
# _color256_to_rgb boundary conditions
# ---------------------------------------------------------------------------


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
