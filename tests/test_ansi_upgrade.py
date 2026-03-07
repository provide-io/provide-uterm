#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for the color-upgrade and preview_ansi additions to provide.terminal.ansi."""

from __future__ import annotations

from provide.terminal.ansi import (
    DEFAULT_PALETTE,
    DEFAULT_RGB,
    preview_ansi,
    upgrade_to_256,
    upgrade_to_truecolor,
)


def test_default_palette_length() -> None:
    assert len(DEFAULT_PALETTE) == 16


def test_default_rgb_length() -> None:
    assert len(DEFAULT_RGB) == 16


def test_default_rgb_tuples() -> None:
    for entry in DEFAULT_RGB:
        r, g, b = entry
        assert 0 <= r <= 255
        assert 0 <= g <= 255
        assert 0 <= b <= 255


# ---------------------------------------------------------------------------
# preview_ansi
# ---------------------------------------------------------------------------


def test_preview_ansi_twgs_tokens() -> None:
    result = preview_ansi("{+r}text{-x}")
    assert "\x1b[" in result
    assert "text" in result
    assert "{+r}" not in result
    assert "{-x}" not in result


def test_preview_ansi_tilde_codes() -> None:
    result = preview_ansi("~1text~0")
    assert "\x1b[" in result
    assert "text" in result
    assert "~1" not in result
    assert "~0" not in result


def test_preview_ansi_pt_tokens() -> None:
    # {P3} is a foreground palette token → SGR code
    result = preview_ansi("{P3}text")
    assert "\x1b[" in result
    assert "text" in result
    assert "{P3}" not in result


def test_preview_ansi_fb_tokens() -> None:
    # {F196} is a 256-color foreground token
    result = preview_ansi("{F196}text")
    assert "\x1b[38;5;196m" in result
    assert "text" in result


def test_preview_ansi_b_token() -> None:
    result = preview_ansi("{B45}text")
    assert "\x1b[48;5;45m" in result


def test_preview_ansi_passthrough_plain() -> None:
    result = preview_ansi("no tokens here")
    assert result == "no tokens here"


# ---------------------------------------------------------------------------
# upgrade_to_256
# ---------------------------------------------------------------------------


def test_upgrade_to_256_sgr() -> None:
    # \x1b[31m is SGR code 31 (red foreground, maps to DEFAULT_PALETTE[1]=160)
    result = upgrade_to_256("\x1b[31mtext\x1b[0m")
    assert "38;5;" in result
    assert "text" in result
    assert "\x1b[31m" not in result


def test_upgrade_to_256_tokens() -> None:
    # {P3} should be replaced with {F<palette[3]>}
    result = upgrade_to_256("{P3}text")
    assert "{P3}" not in result
    assert "{F" in result or "{B" in result


def test_upgrade_to_256_explicit_palette() -> None:
    custom = [10] * 16
    result = upgrade_to_256("\x1b[31mtext", palette=custom)
    assert "38;5;10" in result


def test_upgrade_to_256_palette_default() -> None:
    # None palette uses DEFAULT_PALETTE
    result_none = upgrade_to_256("\x1b[32mtext", palette=None)
    result_default = upgrade_to_256("\x1b[32mtext", palette=DEFAULT_PALETTE)
    assert result_none == result_default


def test_upgrade_to_256_skips_existing_256() -> None:
    # Already-256-color sequences should pass through unchanged
    original = "\x1b[38;5;100mtext"
    result = upgrade_to_256(original)
    assert result == original


# ---------------------------------------------------------------------------
# upgrade_to_truecolor
# ---------------------------------------------------------------------------


def test_upgrade_to_truecolor_sgr() -> None:
    # \x1b[31m → 38;2;R;G;B
    result = upgrade_to_truecolor("\x1b[31mtext")
    assert "38;2;" in result
    assert "text" in result
    assert "\x1b[31m" not in result


def test_upgrade_to_truecolor_palette_default() -> None:
    result_none = upgrade_to_truecolor("\x1b[32mtext", palette=None)
    result_default = upgrade_to_truecolor("\x1b[32mtext", palette=DEFAULT_PALETTE)
    assert result_none == result_default


def test_upgrade_to_truecolor_explicit_palette() -> None:
    # Use a palette where color 1 (red, SGR 31) maps to index 196.
    # _color256_to_rgb(196): idx-16=180, b=0,g=0,r=5 → levels[5]=255 → (255,0,0)
    custom = [0] * 16
    custom[1] = 196
    result = upgrade_to_truecolor("\x1b[31mtext", palette=custom)
    assert "38;2;255;0;0" in result


def test_upgrade_to_truecolor_background() -> None:
    # \x1b[41m is background red (SGR 41 → index 1)
    result = upgrade_to_truecolor("\x1b[41mtext")
    assert "48;2;" in result


def test_upgrade_to_truecolor_skips_existing_tc() -> None:
    original = "\x1b[38;2;100;200;50mtext"
    result = upgrade_to_truecolor(original)
    assert result == original


# ---------------------------------------------------------------------------
# _map_index bright-color branches
# ---------------------------------------------------------------------------


def test_upgrade_to_256_bright_fg_90_range() -> None:
    # bright foreground (90-97) hits _map_index line 162
    result = upgrade_to_256("\x1b[91mtext")
    assert "38;5;" in result


def test_upgrade_to_256_bright_bg_100_range() -> None:
    # bright background (100-107) hits _map_index line 166
    result = upgrade_to_256("\x1b[101mtext")
    assert "48;5;" in result


def test_upgrade_to_256_empty_seq_passthrough() -> None:
    # empty SGR sequence \x1b[m passes through unchanged
    text = "\x1b[m"
    assert upgrade_to_256(text) == text


def test_upgrade_to_256_empty_part_in_seq() -> None:
    # leading semicolon produces an empty part that is skipped
    result = upgrade_to_256("\x1b[;31m")
    assert "38;5;" in result


def test_upgrade_to_256_all_empty_parts() -> None:
    # all-semicolon sequence produces no new_parts → passthrough
    text = "\x1b[;;m"
    assert upgrade_to_256(text) == text


def test_upgrade_to_256_noncolor_code_passthrough() -> None:
    # code 1 (bold) has _map_index → None → preserved as-is
    result = upgrade_to_256("\x1b[1m")
    assert result == "\x1b[1m"


def test_upgrade_to_256_background_color() -> None:
    # code 41 (red bg) hits the "48;5;" branch
    result = upgrade_to_256("\x1b[41m")
    assert "48;5;" in result


# ---------------------------------------------------------------------------
# _convert_sgr_tc branches
# ---------------------------------------------------------------------------


def test_upgrade_to_truecolor_empty_seq_passthrough() -> None:
    text = "\x1b[m"
    assert upgrade_to_truecolor(text) == text


def test_upgrade_to_truecolor_empty_part_in_seq() -> None:
    result = upgrade_to_truecolor("\x1b[;31m")
    assert "38;2;" in result


def test_upgrade_to_truecolor_all_empty_parts() -> None:
    text = "\x1b[;;m"
    assert upgrade_to_truecolor(text) == text


def test_upgrade_to_truecolor_noncolor_code_passthrough() -> None:
    result = upgrade_to_truecolor("\x1b[1m")
    assert result == "\x1b[1m"


def test_upgrade_to_truecolor_t_token() -> None:
    # {T3} is a background token → "48;2;" truecolor
    result = upgrade_to_truecolor("{T3}")
    assert "48;2;" in result


def test_upgrade_to_truecolor_p_token() -> None:
    # {P3} is a foreground token → "38;2;" truecolor
    result = upgrade_to_truecolor("{P3}")
    assert "38;2;" in result


# ---------------------------------------------------------------------------
# _emit_color edge cases (via preview_ansi / _handle_twgs_tokens)
# ---------------------------------------------------------------------------


def test_preview_ansi_unknown_twgs_color_char() -> None:
    # {+z}: unknown color char → _emit_color returns "" → literal fallthrough
    result = preview_ansi("{+z}")
    assert "{" in result  # literal brace preserved


def test_preview_ansi_dim_known_color() -> None:
    # {-r}: polarity "-" with known color → \x1b[0;31m  (line 306 in ansi.py)
    result = preview_ansi("{-r}")
    assert "\x1b[0;31m" in result
