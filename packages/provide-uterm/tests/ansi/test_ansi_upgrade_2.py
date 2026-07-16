#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the color-upgrade and normalize_colors additions to provide.uterm.ansi (part 2)."""

from __future__ import annotations

from provide.uterm.ansi import (
    upgrade_to_256,
    upgrade_to_truecolor,
)


class TestMapIndexDirectBoundaries:
    """Kill _map_index boundary mutations directly.

    mutmut_4: 30 <= code <= 37 → 30 <= code <= 38
    mutmut_18: 40 <= code <= 47 → 40 <= code <= 48
    Code 38 and 48 cannot be tested through upgrade_to_256 because the
    '38 in parts' and '48 in parts' guards intercept them first.
    We call _map_index directly to pin these boundary values.
    """

    def test_map_index_38_is_none(self) -> None:
        """_map_index(38) must return None — kills mutmut_4 (37 → 38 upper bound)."""
        from provide.uterm.ansi import _map_index

        assert _map_index(38) is None, "_map_index(38) must be None (38 is not a valid fg color code)"

    def test_map_index_48_is_none(self) -> None:
        """_map_index(48) must return None — kills mutmut_18 (47 → 48 upper bound)."""
        from provide.uterm.ansi import _map_index

        assert _map_index(48) is None, "_map_index(48) must be None (48 is not a valid bg color code)"

    def test_map_index_37_is_7(self) -> None:
        """_map_index(37) must return 7 — confirms the fg upper bound is exactly 37."""
        from provide.uterm.ansi import _map_index

        assert _map_index(37) == 7

    def test_map_index_47_is_7(self) -> None:
        """_map_index(47) must return 7 — confirms the bg upper bound is exactly 47."""
        from provide.uterm.ansi import _map_index

        assert _map_index(47) == 7


class TestConvertTokensExact:
    """Pin exact token conversion output for upgrade functions."""

    def test_256_p3_produces_f184(self) -> None:
        assert upgrade_to_256("{P3}") == "{F184}"

    def test_256_t3_produces_b184(self) -> None:
        assert upgrade_to_256("{T3}") == "{B184}"

    def test_256_p0_produces_f0(self) -> None:
        assert upgrade_to_256("{P0}") == "{F0}"

    def test_256_t0_produces_b0(self) -> None:
        assert upgrade_to_256("{T0}") == "{B0}"

    def test_256_p8_produces_f244(self) -> None:
        assert upgrade_to_256("{P8}") == "{F244}"

    def test_256_t8_produces_b244(self) -> None:
        assert upgrade_to_256("{T8}") == "{B244}"

    def test_256_p15_produces_f231(self) -> None:
        assert upgrade_to_256("{P15}") == "{F231}"

    def test_256_t15_produces_b231(self) -> None:
        assert upgrade_to_256("{T15}") == "{B231}"

    def test_tc_p3_exact_rgb(self) -> None:
        assert upgrade_to_truecolor("{P3}") == "\x1b[38;2;215;215;0m"

    def test_tc_t3_exact_rgb(self) -> None:
        assert upgrade_to_truecolor("{T3}") == "\x1b[48;2;215;215;0m"

    def test_tc_p0_exact_rgb(self) -> None:
        assert upgrade_to_truecolor("{P0}") == "\x1b[38;2;0;0;0m"

    def test_tc_t8_exact_rgb(self) -> None:
        assert upgrade_to_truecolor("{T8}") == "\x1b[48;2;128;128;128m"


# ---------------------------------------------------------------------------
# Mutant-killing round 2: the stubborn 18
# ---------------------------------------------------------------------------


class TestMultiPartSgr:
    """Multi-code SGR sequences to kill continue→break, join-separator, and
    upper-bound +1 mutations inside _convert_sgr_256 / _convert_sgr_tc."""

    def test_256_bold_plus_red_fg(self) -> None:
        # \x1b[1;31m: code 1 (bold) brightens the fg → bright red (38;5;196)
        # continue→break on code 1 would skip processing code 31
        # ';'.join→'XX;XX'.join would corrupt the separator
        result = upgrade_to_256("\x1b[1;31m")
        assert result == "\x1b[1;38;5;196m"

    def test_256_bold_plus_bg_green(self) -> None:
        # \x1b[1;42m: code 1 (bold) + code 42 (green bg → 48;5;34)
        result = upgrade_to_256("\x1b[1;42m")
        assert result == "\x1b[1;48;5;34m"

    def test_tc_bold_plus_red_fg(self) -> None:
        # \x1b[1;31m → code 1 (bold) brightens → bright red 1;38;2;255;0;0
        result = upgrade_to_truecolor("\x1b[1;31m")
        assert result == "\x1b[1;38;2;255;0;0m"

    def test_tc_bold_plus_bg_green(self) -> None:
        # \x1b[1;42m → code 1 + code 42 → 1;48;2;0;175;0
        result = upgrade_to_truecolor("\x1b[1;42m")
        assert result == "\x1b[1;48;2;0;175;0m"


class TestEmptySeqAndPassthroughGuards:
    """Kill seq=="" → "XXXX" and "48" → "XX48XX" string mutations."""

    def test_256_empty_sgr_exact_match(self) -> None:
        # \x1b[m has seq="" — must return the exact original match
        text = "\x1b[m"
        assert upgrade_to_256(text) == text

    def test_tc_empty_sgr_exact_match(self) -> None:
        text = "\x1b[m"
        assert upgrade_to_truecolor(text) == text

    def test_256_existing_48_passthrough(self) -> None:
        # "48" in parts guard — must pass through unchanged
        text = "\x1b[48;5;100m"
        assert upgrade_to_256(text) == text

    def test_tc_existing_48_passthrough(self) -> None:
        text = "\x1b[48;5;100m"
        assert upgrade_to_truecolor(text) == text

    def test_256_existing_48_2_passthrough(self) -> None:
        text = "\x1b[48;2;10;20;30m"
        assert upgrade_to_256(text) == text

    def test_tc_existing_48_2_passthrough(self) -> None:
        text = "\x1b[48;2;10;20;30m"
        assert upgrade_to_truecolor(text) == text


class TestTokenModulo16:
    """Kill % 16 → % 17 mutations in _convert_tokens_256 / _convert_tokens_tc."""

    def test_256_p16_wraps_to_0(self) -> None:
        # {P16}: 16%16=0 → palette[0]=0 → {F0}
        # With %17: 16%17=16 → palette[16] → IndexError or wrong value
        assert upgrade_to_256("{P16}") == "{F0}"

    def test_256_t16_wraps_to_0(self) -> None:
        assert upgrade_to_256("{T16}") == "{B0}"

    def test_tc_p16_wraps_to_0(self) -> None:
        # {P16}: 16%16=0 → rgb_palette[0] = _color256_to_rgb(0) = (0,0,0)
        assert upgrade_to_truecolor("{P16}") == "\x1b[38;2;0;0;0m"

    def test_tc_t16_wraps_to_0(self) -> None:
        assert upgrade_to_truecolor("{T16}") == "\x1b[48;2;0;0;0m"


class TestBoldSelectsBright:
    """Bold + a foreground color selects the BRIGHT palette entry.

    `\\x1b[1;31m` is bright red in DOS/BBS and in every terminal with
    drawBoldTextInBrightColors (the default). Dropping the bold attribute and
    emitting the dim RGB both mis-renders the color AND fails WCAG contrast on a
    near-black background — the same `{+r}` token then disagrees with the 16- and
    256-color paths, which DO brighten. See uwarp sector-warp numbers (`{+r}`).
    """

    def test_tc_bold_red_is_bright_red(self) -> None:
        # 1;31 -> bright red (255,0,0), not dim red (215,0,0).
        assert upgrade_to_truecolor("\x1b[1;31m") == "\x1b[1;38;2;255;0;0m"

    def test_tc_bold_blue_is_bright_blue_not_cyan(self) -> None:
        # Bright blue must be (0,175,255), NOT (0,255,255) cyan — the
        # DEFAULT_PALETTE[12] bug that a naive idx+8 would otherwise surface.
        assert upgrade_to_truecolor("\x1b[1;34m") == "\x1b[1;38;2;0;175;255m"

    def test_tc_bold_magenta_is_bright_magenta(self) -> None:
        assert upgrade_to_truecolor("\x1b[1;35m") == "\x1b[1;38;2;255;0;255m"

    def test_tc_dim_red_stays_dim(self) -> None:
        # No bold -> unchanged mapping, so {-r} and {+r} stay distinguishable.
        assert upgrade_to_truecolor("\x1b[0;31m") == "\x1b[0;38;2;215;0;0m"
        assert upgrade_to_truecolor("\x1b[31m") == "\x1b[38;2;215;0;0m"

    def test_tc_bold_does_not_brighten_background(self) -> None:
        # Bold is a foreground attribute; 1;41 keeps the dim red BACKGROUND.
        assert upgrade_to_truecolor("\x1b[1;41m") == "\x1b[1;48;2;215;0;0m"

    def test_tc_bold_with_already_bright_fg_is_not_double_bumped(self) -> None:
        # 90-97 are already bright; bold must not push them out of range.
        assert upgrade_to_truecolor("\x1b[1;91m") == "\x1b[1;38;2;255;0;0m"

    def test_256_bold_red_is_bright_red(self) -> None:
        # The 256-color path (bbsbot) must brighten identically: 196, not 160.
        assert upgrade_to_256("\x1b[1;31m") == "\x1b[1;38;5;196m"

    def test_256_bold_blue_is_bright_blue(self) -> None:
        assert upgrade_to_256("\x1b[1;34m") == "\x1b[1;38;5;39m"
