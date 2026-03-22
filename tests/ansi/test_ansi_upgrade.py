#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for the color-upgrade and normalize_colors additions to provide.terminal.ansi."""

from __future__ import annotations

from provide.terminal import registered_dialects
from provide.terminal.ansi import (
    DEFAULT_PALETTE,
    DEFAULT_RGB,
    _color256_to_rgb,
    _emit_color,
    _handle_brace_tokens,
    _handle_extended_tokens,
    _handle_tilde_codes,
    normalize_colors,
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
# normalize_colors
# ---------------------------------------------------------------------------


def test_normalize_colors_tilde_codes() -> None:
    result = normalize_colors("~1text~0")
    assert "\x1b[" in result
    assert "text" in result
    assert "~1" not in result
    assert "~0" not in result


def test_normalize_colors_pt_tokens() -> None:
    # {P3} is a foreground palette token → SGR code
    result = normalize_colors("{P3}text")
    assert "\x1b[" in result
    assert "text" in result
    assert "{P3}" not in result


def test_normalize_colors_fb_tokens() -> None:
    # {F196} is a 256-color foreground token
    result = normalize_colors("{F196}text")
    assert "\x1b[38;5;196m" in result
    assert "text" in result


def test_normalize_colors_b_token() -> None:
    result = normalize_colors("{B45}text")
    assert "\x1b[48;5;45m" in result


def test_normalize_colors_passthrough_plain() -> None:
    result = normalize_colors("no tokens here")
    assert result == "no tokens here"


def test_normalize_colors_pipe_codes() -> None:
    result = normalize_colors("|04red|00")
    assert "\x1b[31m" in result
    assert "\x1b[30m" in result


def test_normalize_colors_brace_tokens() -> None:
    result = normalize_colors("{+Bw}Daily Journal{NK}")
    assert "\x1b[1;37m" in result
    assert "\x1b[0m" in result
    assert "{+Bw}" not in result
    assert "{NK}" not in result


def test_brace_tokens_registered_builtin() -> None:
    assert "brace_tokens" in registered_dialects()


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
# _emit_color edge cases (via _handle_brace_tokens)
# ---------------------------------------------------------------------------


def test_brace_tokens_unknown_color_char() -> None:
    # {+z}: unknown color char → _emit_color returns "" → literal fallthrough
    result = _handle_brace_tokens("{+z}")
    assert "{" in result  # literal brace preserved


def test_brace_tokens_dim_known_color() -> None:
    # {-r}: polarity "-" with known color → \x1b[0;31m
    result = _handle_brace_tokens("{-r}")
    assert "\x1b[0;31m" in result


# ---------------------------------------------------------------------------
# Mutant-killing tests: exact boundary + value assertions
# ---------------------------------------------------------------------------


class TestMapIndexExactBoundaries:
    """Pin exact SGR -> 256/truecolor output to kill off-by-one boundary mutations."""

    def test_256_fg_black_code30(self) -> None:
        assert upgrade_to_256("\x1b[30m") == "\x1b[38;5;0m"

    def test_256_fg_white_code37(self) -> None:
        assert upgrade_to_256("\x1b[37m") == "\x1b[38;5;252m"

    def test_256_bright_fg_black_code90(self) -> None:
        assert upgrade_to_256("\x1b[90m") == "\x1b[38;5;244m"

    def test_256_bright_fg_white_code97(self) -> None:
        assert upgrade_to_256("\x1b[97m") == "\x1b[38;5;231m"

    def test_256_bg_black_code40(self) -> None:
        assert upgrade_to_256("\x1b[40m") == "\x1b[48;5;0m"

    def test_256_bg_white_code47(self) -> None:
        assert upgrade_to_256("\x1b[47m") == "\x1b[48;5;252m"

    def test_256_bright_bg_black_code100(self) -> None:
        assert upgrade_to_256("\x1b[100m") == "\x1b[48;5;244m"

    def test_256_bright_bg_white_code107(self) -> None:
        assert upgrade_to_256("\x1b[107m") == "\x1b[48;5;231m"

    def test_tc_fg_black_code30(self) -> None:
        assert upgrade_to_truecolor("\x1b[30m") == "\x1b[38;2;0;0;0m"

    def test_tc_fg_white_code37(self) -> None:
        assert upgrade_to_truecolor("\x1b[37m") == "\x1b[38;2;208;208;208m"

    def test_tc_bright_fg_black_code90(self) -> None:
        assert upgrade_to_truecolor("\x1b[90m") == "\x1b[38;2;128;128;128m"

    def test_tc_bright_fg_white_code97(self) -> None:
        assert upgrade_to_truecolor("\x1b[97m") == "\x1b[38;2;255;255;255m"

    def test_tc_bg_black_code40(self) -> None:
        assert upgrade_to_truecolor("\x1b[40m") == "\x1b[48;2;0;0;0m"

    def test_tc_bg_white_code47(self) -> None:
        assert upgrade_to_truecolor("\x1b[47m") == "\x1b[48;2;208;208;208m"

    def test_tc_bright_bg_black_code100(self) -> None:
        assert upgrade_to_truecolor("\x1b[100m") == "\x1b[48;2;128;128;128m"

    def test_tc_bright_bg_white_code107(self) -> None:
        assert upgrade_to_truecolor("\x1b[107m") == "\x1b[48;2;255;255;255m"

    def test_256_code29_not_mapped(self) -> None:
        assert upgrade_to_256("\x1b[29m") == "\x1b[29m"

    def test_256_code39_not_mapped(self) -> None:
        assert upgrade_to_256("\x1b[39m") == "\x1b[39m"

    def test_256_code89_not_mapped(self) -> None:
        assert upgrade_to_256("\x1b[89m") == "\x1b[89m"

    def test_256_code98_not_mapped(self) -> None:
        assert upgrade_to_256("\x1b[98m") == "\x1b[98m"

    def test_256_code99_not_mapped(self) -> None:
        assert upgrade_to_256("\x1b[99m") == "\x1b[99m"

    def test_256_code108_not_mapped(self) -> None:
        assert upgrade_to_256("\x1b[108m") == "\x1b[108m"

    def test_256_fg_vs_bg_exact(self) -> None:
        assert upgrade_to_256("\x1b[30m") == "\x1b[38;5;0m"
        assert upgrade_to_256("\x1b[40m") == "\x1b[48;5;0m"

    def test_tc_fg_vs_bg_exact(self) -> None:
        assert upgrade_to_truecolor("\x1b[30m") == "\x1b[38;2;0;0;0m"
        assert upgrade_to_truecolor("\x1b[40m") == "\x1b[48;2;0;0;0m"


class TestHandleExtendedTokensExact:
    """Pin exact P/T token output to kill string and boundary mutations."""

    def test_p0_fg_non_bright(self) -> None:
        assert _handle_extended_tokens("{P0}") == "\x1b[30m"

    def test_p7_fg_non_bright_upper(self) -> None:
        assert _handle_extended_tokens("{P7}") == "\x1b[37m"

    def test_p8_fg_bright_lower(self) -> None:
        assert _handle_extended_tokens("{P8}") == "\x1b[90m"

    def test_p15_fg_bright_upper(self) -> None:
        assert _handle_extended_tokens("{P15}") == "\x1b[97m"

    def test_t0_bg_non_bright(self) -> None:
        assert _handle_extended_tokens("{T0}") == "\x1b[40m"

    def test_t7_bg_non_bright_upper(self) -> None:
        assert _handle_extended_tokens("{T7}") == "\x1b[47m"

    def test_t8_bg_bright_lower(self) -> None:
        assert _handle_extended_tokens("{T8}") == "\x1b[100m"

    def test_t15_bg_bright_upper(self) -> None:
        assert _handle_extended_tokens("{T15}") == "\x1b[107m"

    def test_p_modulo_wraps(self) -> None:
        assert _handle_extended_tokens("{P16}") == "\x1b[30m"

    def test_t_modulo_wraps(self) -> None:
        assert _handle_extended_tokens("{T16}") == "\x1b[40m"


class TestEmitColorExact:
    """Pin exact _emit_color return values."""

    def test_reset_exact(self) -> None:
        assert _emit_color("-", "x") == "\x1b[0m"

    def test_bright_fg_exact(self) -> None:
        assert _emit_color("+", "r") == "\x1b[0;1;31m"

    def test_dim_fg_exact(self) -> None:
        assert _emit_color("-", "r") == "\x1b[0;31m"

    def test_unknown_char_empty(self) -> None:
        assert _emit_color("+", "z") == ""


class TestColor256ToRgbLevels:
    """Pin exact RGB values for 256-color cube to kill levels-tuple constant mutations."""

    def test_index_17_levels_1_blue(self) -> None:
        assert _color256_to_rgb(17) == (0, 0, 95)

    def test_index_18_levels_2_blue(self) -> None:
        assert _color256_to_rgb(18) == (0, 0, 135)

    def test_index_19_levels_3_blue(self) -> None:
        assert _color256_to_rgb(19) == (0, 0, 175)

    def test_index_20_levels_4_blue(self) -> None:
        assert _color256_to_rgb(20) == (0, 0, 215)

    def test_index_21_levels_5_blue(self) -> None:
        assert _color256_to_rgb(21) == (0, 0, 255)

    def test_index_22_levels_1_green(self) -> None:
        assert _color256_to_rgb(22) == (0, 95, 0)

    def test_index_88_levels_2_red(self) -> None:
        assert _color256_to_rgb(88) == (135, 0, 0)

    def test_index_112_levels_mixed(self) -> None:
        assert _color256_to_rgb(112) == (135, 215, 0)


class TestHandleBraceTokensExact:
    """Pin exact brace token output and boundary passthrough."""

    def test_plus_r_exact(self) -> None:
        assert _handle_brace_tokens("{+r}") == "\x1b[1;31m"

    def test_minus_x_exact(self) -> None:
        assert _handle_brace_tokens("{-x}") == "\x1b[0m"

    def test_minus_r_exact(self) -> None:
        assert _handle_brace_tokens("{-r}") == "\x1b[0;31m"

    def test_truncated_no_closing_brace(self) -> None:
        assert _handle_brace_tokens("{+r") == "{+r"

    def test_brace_as_only_char(self) -> None:
        assert _handle_brace_tokens("{") == "{"

    def test_invalid_polarity_passthrough(self) -> None:
        assert _handle_brace_tokens("{xr}") == "{xr}"

    def test_surrounding_text_preserved(self) -> None:
        assert _handle_brace_tokens("A{+r}B") == "A\x1b[1;31mB"

    def test_3char_token_bold(self) -> None:
        assert _handle_brace_tokens("{T}") == "\x1b[1m"

    def test_3char_token_reset(self) -> None:
        assert _handle_brace_tokens("{t}") == "\x1b[0m"


class TestHandleTildeCodesExact:
    """Pin exact tilde code output and boundary passthrough."""

    def test_tilde_1_exact(self) -> None:
        assert _handle_tilde_codes("~1") == "\x1b[0;1;32m"

    def test_tilde_at_end_passthrough(self) -> None:
        assert _handle_tilde_codes("~") == "~"

    def test_tilde_0_exact(self) -> None:
        assert _handle_tilde_codes("~0") == "\x1b[0m"

    def test_tilde_7_exact(self) -> None:
        assert _handle_tilde_codes("~7") == "\x1b[0;37m"

    def test_tilde_unknown_passthrough(self) -> None:
        assert _handle_tilde_codes("~Z") == "~Z"

    def test_tilde_in_context(self) -> None:
        assert _handle_tilde_codes("A~ZB") == "A~ZB"


# (TestMapIndexDirectBoundaries, TestConvertTokensExact, TestMultiPartSgr,
#  TestEmptySeqAndPassthroughGuards, TestTokenModulo16 moved to test_ansi_upgrade_2.py)
