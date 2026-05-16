#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.render.sgr — SGR escape sequence emitters."""

from __future__ import annotations

from provide.uterm.render.sgr import (
    SGR_FUNCTIONS,
    ColorMode,
    sgr_16,
    sgr_256,
    sgr_truecolor,
)

# ---------------------------------------------------------------------------
# sgr_truecolor
# ---------------------------------------------------------------------------


def test_sgr_truecolor_black_black() -> None:
    result = sgr_truecolor((0, 0, 0), (0, 0, 0))
    assert result == "\x1b[38;2;0;0;0;48;2;0;0;0m"


def test_sgr_truecolor_red_fg_blue_bg() -> None:
    result = sgr_truecolor((255, 0, 0), (0, 0, 255))
    assert result == "\x1b[38;2;255;0;0;48;2;0;0;255m"


def test_sgr_truecolor_all_channels_distinct() -> None:
    result = sgr_truecolor((10, 20, 30), (40, 50, 60))
    assert result == "\x1b[38;2;10;20;30;48;2;40;50;60m"


def test_sgr_truecolor_fg_g_not_swapped_with_b() -> None:
    result = sgr_truecolor((1, 2, 3), (4, 5, 6))
    assert "38;2;1;2;3" in result


def test_sgr_truecolor_bg_r_not_swapped_with_g() -> None:
    result = sgr_truecolor((1, 2, 3), (4, 5, 6))
    assert "48;2;4;5;6" in result


# ---------------------------------------------------------------------------
# sgr_256
# ---------------------------------------------------------------------------


def test_sgr_256_format() -> None:
    result = sgr_256((255, 0, 0), (0, 0, 255))
    assert result.startswith("\x1b[38;5;")
    assert ";48;5;" in result
    assert result.endswith("m")


def test_sgr_256_fg_index_correct() -> None:
    result = sgr_256((255, 0, 0), (0, 0, 0))
    assert "38;5;196" in result


def test_sgr_256_bg_index_correct() -> None:
    result = sgr_256((255, 0, 0), (0, 0, 0))
    assert "48;5;0" in result


# ---------------------------------------------------------------------------
# sgr_16
# ---------------------------------------------------------------------------


def test_sgr_16_format() -> None:
    result = sgr_16((255, 0, 0), (0, 0, 0))
    assert result.startswith("\x1b[")
    assert result.endswith("m")
    inner = result[2:-1]
    parts = inner.split(";")
    assert len(parts) == 2
    assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# SGR_FUNCTIONS dict
# ---------------------------------------------------------------------------


def test_sgr_functions_contains_all_modes() -> None:
    assert set(SGR_FUNCTIONS.keys()) == {"truecolor", "256", "16"}


def test_sgr_functions_truecolor_is_sgr_truecolor() -> None:
    assert SGR_FUNCTIONS["truecolor"] is sgr_truecolor


def test_sgr_functions_256_is_sgr_256() -> None:
    assert SGR_FUNCTIONS["256"] is sgr_256


def test_sgr_functions_16_is_sgr_16() -> None:
    assert SGR_FUNCTIONS["16"] is sgr_16


# ---------------------------------------------------------------------------
# ColorMode type alias
# ---------------------------------------------------------------------------


def test_color_mode_is_literal() -> None:
    # Just verify it's importable and is a type alias
    mode: ColorMode = "truecolor"
    assert mode == "truecolor"
