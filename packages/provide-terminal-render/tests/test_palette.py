#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.render.palette — color tables and quantizers."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from provide.terminal.render.palette import (
    ANSI16_PALETTE,
    _color_dist_sq,
    nearest_16,
    nearest_256,
)

# ---------------------------------------------------------------------------
# _color_dist_sq — all three terms must contribute independently
# ---------------------------------------------------------------------------


def test_color_dist_sq_red_channel() -> None:
    assert _color_dist_sq(10, 0, 0, 0, 0, 0) == 100


def test_color_dist_sq_green_channel() -> None:
    assert _color_dist_sq(0, 10, 0, 0, 0, 0) == 100


def test_color_dist_sq_blue_channel() -> None:
    assert _color_dist_sq(0, 0, 10, 0, 0, 0) == 100


def test_color_dist_sq_all_channels() -> None:
    assert _color_dist_sq(3, 4, 5, 0, 0, 0) == 50


def test_color_dist_sq_exponent_is_2() -> None:
    assert _color_dist_sq(3, 0, 0, 0, 0, 0) == 9


def test_color_dist_sq_symmetric() -> None:
    assert _color_dist_sq(10, 20, 30, 5, 10, 15) == _color_dist_sq(5, 10, 15, 10, 20, 30)


# ---------------------------------------------------------------------------
# nearest_16
# ---------------------------------------------------------------------------


def test_nearest_16_pure_red() -> None:
    fg, bg = nearest_16(255, 0, 0)
    assert fg == 31
    assert bg == 41


def test_nearest_16_bright_red() -> None:
    fg, bg = nearest_16(255, 85, 85)
    assert fg == 91
    assert bg == 101


def test_nearest_16_pure_black() -> None:
    fg, bg = nearest_16(0, 0, 0)
    assert fg == 30
    assert bg == 40


def test_nearest_16_loop_includes_index_0() -> None:
    fg, bg = nearest_16(0, 0, 0)
    assert fg == 30
    assert bg == 40


def test_nearest_16_second_entry_is_best() -> None:
    fg, bg = nearest_16(160, 0, 0)
    assert fg == 31
    assert bg == 41


def test_nearest_16_tie_broken_by_strict_less_than() -> None:
    fg, bg = nearest_16(85, 0, 0)
    assert fg == 30
    assert bg == 40


# ---------------------------------------------------------------------------
# nearest_256
# ---------------------------------------------------------------------------


def test_nearest_256_pure_red() -> None:
    assert nearest_256(255, 0, 0) == 196


def test_nearest_256_pure_white() -> None:
    assert nearest_256(255, 255, 255) == 15


def test_nearest_256_pure_black() -> None:
    assert nearest_256(0, 0, 0) == 0


def test_nearest_256_index_1_best() -> None:
    assert nearest_256(170, 0, 0) == 1


def test_nearest_256_loop_covers_index_1() -> None:
    assert nearest_256(170, 0, 0) == 1


# ---------------------------------------------------------------------------
# _build_xterm256 — palette construction
# ---------------------------------------------------------------------------


def _fresh_build() -> None:
    """Clear _XTERM256 and rebuild."""
    import provide.terminal.render.palette as pal_mod

    pal_mod._XTERM256.clear()
    pal_mod._build_xterm256()


@pytest.fixture(autouse=False)
def fresh_xterm256() -> Generator[None, None, None]:
    import provide.terminal.render.palette as pal_mod

    original = list(pal_mod._XTERM256)
    pal_mod._XTERM256.clear()
    pal_mod._build_xterm256()
    yield
    pal_mod._XTERM256.clear()
    pal_mod._XTERM256.extend(original)


def test_build_xterm256_length() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert len(pal_mod._XTERM256) == 256


def test_build_xterm256_first_16_match_ansi16() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    for idx, (r, g, b, _fg, _bg) in enumerate(ANSI16_PALETTE):
        assert pal_mod._XTERM256[idx] == (r, g, b), f"index {idx} mismatch"


def test_build_xterm256_index_16_is_000() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[16] == (0, 0, 0)


def test_build_xterm256_index_17_is_00_95() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[17] == (0, 0, 95)


def test_build_xterm256_index_231_is_255_255_255() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[231] == (255, 255, 255)


def test_build_xterm256_cube_row_r_nonzero() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[52] == (95, 0, 0)


def test_build_xterm256_cube_row_g_nonzero() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[22] == (0, 95, 0)


def test_build_xterm256_cube_row_b_nonzero() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[17][2] == 95


def test_build_xterm256_grayscale_first() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[232] == (8, 8, 8)


def test_build_xterm256_grayscale_last() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[255] == (238, 238, 238)


def test_build_xterm256_grayscale_mid() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    assert pal_mod._XTERM256[244] == (128, 128, 128)


def test_build_xterm256_idempotent() -> None:
    _fresh_build()
    import provide.terminal.render.palette as pal_mod

    pal_mod._build_xterm256()
    assert len(pal_mod._XTERM256) == 256


def test_ansi16_palette_length() -> None:
    assert len(ANSI16_PALETTE) == 16


def test_ansi16_palette_first_entry() -> None:
    assert ANSI16_PALETTE[0] == (0, 0, 0, 30, 40)


def test_ansi16_palette_last_entry() -> None:
    assert ANSI16_PALETTE[15] == (255, 255, 255, 97, 107)
