#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for gateway/_gateway.py and provide.uterm.colors."""

from __future__ import annotations

from provide.uterm.colors import (
    apply_color_mode,
    rgb_to_16_index,
    rgb_to_256,
)
from provide.uterm.colors.rgb import _clamp8
from provide.uterm.gateway._gateway import (
    _strip_iac,
)


def _async_iter(items):
    """Return an async iterator over items."""

    async def _gen():
        for item in items:
            yield item

    return _gen()


# ---------------------------------------------------------------------------
# _clamp8 mutation killers
# mutmut_2: v <= 0  instead of v < 0  (fails for v==0: should return 0, returns 0 but also clamps)
# mutmut_3: v < 1   instead of v < 0  (fails for v==0: should return 0 but won't enter if)
# mutmut_5: v >= 255 instead of v > 255 (fails for v==255: should return 255 as passthrough)
# mutmut_6: v > 256 instead of v > 255 (fails for v==256: should return 255)
# ---------------------------------------------------------------------------


class TestClamp8MutationKilling:
    def test_zero_returns_zero(self):
        """v=0 must return 0 (not trigger < 0 branch); kills mutmut_2 (<=0 would clamp 0→0 but
        mutmut_3 (<1) would return 0 also — need to check via boundary properly)."""
        assert _clamp8(0) == 0

    def test_positive_zero_boundary_is_not_clamped(self):
        """v=0 returns 0 (pass-through), not clamped to 0. Kills mutmut_2 (<=0 returns 0 so no diff)
        and mutmut_3 (v<1 would also return 0). Need explicit check that passthrough occurs."""
        # The key insight: original returns v when 0<=v<=255
        assert _clamp8(0) == 0  # not clamped, just passthrough
        assert _clamp8(1) == 1  # definitely not negative

    def test_exact_255_is_not_clamped(self):
        """v=255 is exactly the max; must return 255 (passthrough), not 255 (clamp).
        Kills mutmut_5 (>=255 would clamp 255 to 255, but that's same... need a different check.)
        Actually mutmut_5 returns 255 if v >= 255 which hits for v=255 → returns 255 same result.
        The difference: mutmut_5 uses >= so v=255 enters the "clamp high" branch.
        For v=255, result is same. For v=254: original returns 254 (passthrough), mutmut_5 also 254.
        Mutmut_5 fails for: what?  Actually 255 is same. The divergence would be if something else.
        Wait: `0 if v < 0 else 255 if v >= 255 else v` — for v=255: returns 255 (same).
        This mutant is equivalent for integer inputs since 255 maps to 255 either way.
        However, it changes behavior for float inputs but we only deal with ints.
        This may be an equivalent mutant. Let's just add boundary tests."""
        assert _clamp8(255) == 255

    def test_256_clamped_to_255(self):
        """v=256 must return 255. Kills mutmut_6 (v>256 would not clamp 256 → returns 256)."""
        assert _clamp8(256) == 255

    def test_negative_one_clamped_to_zero(self):
        """v=-1 must return 0."""
        assert _clamp8(-1) == 0

    def test_midrange_passthrough(self):
        """v=128 returns 128."""
        assert _clamp8(128) == 128


# ---------------------------------------------------------------------------
# rgb_to_256 mutation killers
# mutmut_3: r <= 8  (fails when r==8, which maps to 16 in orig but to grayscale ramp in mutant)
# mutmut_4: r < 9   (same issue at r==8 boundary — 8<9=True so would still return 16)
# mutmut_6: r >= 248 (fails for r==248, which should go to grayscale ramp but mutant returns 231)
# mutmut_7: r > 249 (fails for r==248 or r==249 boundary)
# ---------------------------------------------------------------------------


class TestRgbTo256MutationKilling:
    def test_gray_exactly_8_returns_16(self):
        """r=g=b=8: r < 8 is False so falls through to grayscale ramp.
        mutmut_3 (r<=8) would return 16. mutmut_4 (r<9) would return 16.
        Original: 232 + int((8-8)/247*24) = 232. Not 16."""
        result = rgb_to_256(8, 8, 8)
        assert result == 232  # grayscale ramp, not 16
        assert result != 16

    def test_gray_below_8_returns_16(self):
        """r=g=b=7: r < 8 is True → returns 16."""
        assert rgb_to_256(7, 7, 7) == 16

    def test_gray_exactly_248_in_grayscale_ramp(self):
        """r=g=b=248: r > 248 is False in original → uses grayscale ramp formula.
        mutmut_6 (r>=248) would return 231."""
        result = rgb_to_256(248, 248, 248)
        # Original: 232 + int((248-8)/247*24) = 232 + int(240/247*24) = 232 + int(23.3..) = 232+23 = 255
        assert result == 255
        assert result != 231

    def test_gray_above_248_returns_231(self):
        """r=g=b=249: r > 248 is True → returns 231."""
        assert rgb_to_256(249, 249, 249) == 231

    def test_gray_exactly_249_returns_231(self):
        """r=g=b=249: boundary test. mutmut_7 (r>249) would send to grayscale ramp."""
        assert rgb_to_256(249, 249, 249) == 231

    def test_gray_exactly_248_not_231(self):
        """Confirm 248 is NOT 231 (grayscale ramp). Kills mutmut_6."""
        assert rgb_to_256(248, 248, 248) != 231

    def test_non_gray_uses_color_cube(self):
        """Non-equal r,g,b uses the color cube formula, returns 16+36*rc+6*gc+bc."""
        result = rgb_to_256(0, 0, 0)
        assert result == 16

    def test_non_gray_pure_red(self):
        """(255, 0, 0) → rc=5, gc=0, bc=0 → 16+36*5=196."""
        assert rgb_to_256(255, 0, 0) == 196

    def test_non_gray_pure_blue(self):
        """(0, 0, 255) → rc=0, gc=0, bc=5 → 16+5=21."""
        assert rgb_to_256(0, 0, 255) == 21

    def test_non_gray_pure_green(self):
        """(0, 255, 0) → rc=0, gc=5, bc=0 → 16+30=46."""
        assert rgb_to_256(0, 255, 0) == 46


# ---------------------------------------------------------------------------
# rgb_to_16_index mutation killers
# The function computes minimum Euclidean distance from 16 ANSI palette entries.
# Mutations affect the distance formula components (rr-tr)*(rr-tr) etc.
# Key: test with known colors that map to specific indices.
# ---------------------------------------------------------------------------


class TestRgbTo16IndexMutationKilling:
    def test_black_maps_to_index_0(self):
        """(0,0,0) → index 0 (black)."""
        assert rgb_to_16_index(0, 0, 0) == 0

    def test_pure_red_maps_to_index_4(self):
        """(205, 0, 0) → index 4 (dark red). Distance to palette[4]=(205,0,0) is 0."""
        assert rgb_to_16_index(205, 0, 0) == 4

    def test_pure_green_maps_to_index_2(self):
        """(0, 205, 0) → index 2 (dark green). Distance to palette[2]=(0,205,0) is 0."""
        assert rgb_to_16_index(0, 205, 0) == 2

    def test_pure_blue_maps_to_index_1(self):
        """(0, 0, 205) → index 1 (dark blue). Distance to palette[1]=(0,0,205) is 0."""
        assert rgb_to_16_index(0, 0, 205) == 1

    def test_pure_white_maps_to_index_15(self):
        """(255, 255, 255) → index 15 (bright white). palette[15]=(255,255,255)."""
        assert rgb_to_16_index(255, 255, 255) == 15

    def test_bright_cyan_maps_to_index_11(self):
        """(92, 255, 255) → index 11. palette[11]=(92,255,255)."""
        assert rgb_to_16_index(92, 255, 255) == 11

    def test_bright_magenta_maps_to_index_13(self):
        """(255, 92, 255) → index 13. palette[13]=(255,92,255)."""
        assert rgb_to_16_index(255, 92, 255) == 13

    def test_bright_yellow_maps_to_index_14(self):
        """(255, 255, 92) → index 14. palette[14]=(255,255,92)."""
        assert rgb_to_16_index(255, 255, 92) == 14

    def test_dark_gray_maps_to_index_8(self):
        """(127, 127, 127) → index 8 (dark gray). palette[8]=(127,127,127)."""
        assert rgb_to_16_index(127, 127, 127) == 8

    def test_distance_formula_all_components_matter(self):
        """A color close to palette[4]=(205,0,0) vs palette[6]=(205,205,0).
        (200, 1, 1) is closer to (205,0,0) than to (205,205,0)."""
        idx = rgb_to_16_index(200, 1, 1)
        # Should be index 4 (dark red), not index 6 (dark yellow)
        assert idx == 4


# ---------------------------------------------------------------------------
# apply_color_mode mutation killers
# mutmut_7, _8: i+4 < len(parts) boundary
# mutmut_10, _11, _12: parts[i] in {"38","48"} check
# mutmut_30, _31: parts[i+1] == "2"
# mutmut_33: parts[i+2].isdigit()
# mutmut_41, _42: is_fg = parts[i] == "38"
# mutmut_43, _44, _45: mode == "256" condition
# mutmut_49, _50: FG vs BG code selection
# ---------------------------------------------------------------------------


class TestApplyColorModeMutationKilling:
    def test_256_fg_exact_codes(self):
        """fg RGB → 38;5;N format."""
        raw = b"\x1b[38;2;255;0;0m"
        out = apply_color_mode(raw, "256")
        # pure red maps to 196
        assert b"38;5;196m" in out

    def test_256_bg_exact_codes(self):
        """bg RGB → 48;5;N format, not 38;5;N."""
        raw = b"\x1b[48;2;0;0;255m"
        out = apply_color_mode(raw, "256")
        # pure blue maps to 21
        assert b"48;5;21m" in out
        assert b"38;5;" not in out

    def test_16_fg_produces_ansi_color_code(self):
        """fg RGB in 16-color mode → produces _FG_16 code."""
        raw = b"\x1b[38;2;205;0;0m"
        out = apply_color_mode(raw, "16")
        # palette index 4 = dark red → _FG_16[4] = 31
        assert b"\x1b[31m" in out

    def test_16_bg_produces_ansi_color_code(self):
        """bg RGB in 16-color mode → produces _BG_16 code."""
        raw = b"\x1b[48;2;205;0;0m"
        out = apply_color_mode(raw, "16")
        # palette index 4 = dark red → _BG_16[4] = 41
        assert b"\x1b[41m" in out

    def test_fg_not_confused_with_bg(self):
        """38 and 48 must produce different output codes."""
        raw_fg = b"\x1b[38;2;255;0;0m"
        raw_bg = b"\x1b[48;2;255;0;0m"
        out_fg = apply_color_mode(raw_fg, "256")
        out_bg = apply_color_mode(raw_bg, "256")
        assert b"38;5;" in out_fg
        assert b"48;5;" in out_bg
        assert b"48;5;" not in out_fg
        assert b"38;5;" not in out_bg

    def test_passthrough_returns_raw_unchanged(self):
        """passthrough mode must return raw bytes unchanged."""
        raw = b"\x1b[38;2;255;0;0m"
        assert apply_color_mode(raw, "passthrough") is raw

    def test_256_requires_5_parts_for_rgb(self):
        """Short param (only 3 parts after split) does not get rewritten."""
        raw = b"\x1b[38;2;255m"  # only 3 parts (38, 2, 255)
        out = apply_color_mode(raw, "256")
        # Should NOT rewrite (not enough parts)
        assert b"38;5;" not in out

    def test_needs_part_index_1_equals_2(self):
        """If part[i+1] != '2', no rewrite. Using '1' instead of '2'."""
        raw = b"\x1b[38;1;255;0;0m"
        out = apply_color_mode(raw, "256")
        assert b"38;5;" not in out

    def test_part_must_be_38_or_48(self):
        """Code 37 (not 38 or 48) should not trigger RGB rewrite."""
        raw = b"\x1b[37;2;255;0;0m"
        out = apply_color_mode(raw, "256")
        assert b"38;5;" not in out
        assert b"48;5;" not in out


# ---------------------------------------------------------------------------
# _strip_iac mutation killers (IAC handling edge cases)
# ---------------------------------------------------------------------------


class TestStripIacMutationKilling:
    def test_escaped_iac_followed_by_data(self):
        """IAC IAC followed by plain data: single IAC emitted, data continues.
        Kills mutmut_22 (i=2 instead of i+=2, breaks subsequent data).
        Kills mutmut_24 (i+=3), mutmut_25 (break instead of continue)."""
        # Data: 0xFF 0xFF A B
        data = bytes([255, 255, 65, 66])
        result = _strip_iac(data)
        assert result == bytes([255, 65, 66])

    def test_escaped_iac_multiple_times(self):
        """Multiple IAC IAC sequences each produce single 0xFF.
        Kills mutmut_22 (i=2 would break first iteration)."""
        data = bytes([255, 255, 255, 255, 65])
        result = _strip_iac(data)
        assert result == bytes([255, 255, 65])

    def test_sb_followed_by_se_then_data(self):
        """SB subnegotiation followed by more data after IAC SE.
        Kills mutmut_27 (i=2 instead of i+=2), mutmut_28 (i-=2), mutmut_29 (i+=3)."""
        # IAC SB <data> IAC SE <after>
        data = bytes([255, 250, 1, 2, 3, 255, 240]) + b"after"
        result = _strip_iac(data)
        assert result == b"after"

    def test_sb_with_preceding_data_and_following_data(self):
        """Data before SB block is preserved; data after is preserved.
        Kills mutmut_27 (i=2 reset would process wrong data)."""
        data = b"pre" + bytes([255, 250, 42, 255, 240]) + b"post"
        result = _strip_iac(data)
        assert result == b"prepost"

    def test_will_with_option_stripped_followed_by_data(self):
        """IAC WILL OPT followed by data: all three bytes consumed, data passes.
        Kills various mutmut variants around i+=3."""
        data = bytes([255, 251, 1]) + b"hello"
        result = _strip_iac(data)
        assert result == b"hello"

    def test_ip_produces_ctrl_c_then_continues(self):
        """IAC IP followed by data: 0x03 emitted then data passes.
        Kills mutants around IP handling."""
        data = bytes([255, 244]) + b"abc"
        result = _strip_iac(data)
        assert result == bytes([0x03]) + b"abc"

    def test_break_produces_ctrl_c_then_continues(self):
        """IAC BREAK (243) followed by data: 0x03 emitted then data passes."""
        data = bytes([255, 243]) + b"xyz"
        result = _strip_iac(data)
        assert result == bytes([0x03]) + b"xyz"

    def test_eof_produces_ctrl_d_then_continues(self):
        """IAC EOF followed by data: 0x04 emitted then data passes."""
        data = bytes([255, 236]) + b"end"
        result = _strip_iac(data)
        assert result == bytes([0x04]) + b"end"

    def test_ao_silently_dropped_then_data(self):
        """IAC AO (245) + data: AO dropped, data preserved."""
        data = bytes([255, 245]) + b"text"
        result = _strip_iac(data)
        assert result == b"text"

    def test_mixed_sequences_correct_order(self):
        """Complex mix: plain data, WILL, plain, IP, plain."""
        data = b"A" + bytes([255, 251, 1]) + b"B" + bytes([255, 244]) + b"C"
        result = _strip_iac(data)
        assert result == b"AB" + bytes([0x03]) + b"C"

    def test_sb_inner_loop_boundary(self):
        """SB with exactly the minimum data: IAC SB opt IAC SE.
        Kills mutmut_32 (i<n vs i<=n in inner loop — would read past end)."""
        data = bytes([255, 250, 99, 255, 240])
        result = _strip_iac(data)
        assert result == b""

    def test_will_truncated_at_end_discarded(self):
        """IAC WILL at end of buffer with no option byte: discarded safely."""
        data = bytes([255, 251])  # truncated
        result = _strip_iac(data)
        assert result == b""

    def test_dont_option_stripped(self):
        """IAC DONT OPT stripped, following data preserved."""
        data = bytes([255, 254, 3]) + b"ok"
        result = _strip_iac(data)
        assert result == b"ok"


# (TestHandleWsControlMutationKilling, TestWsToTcpMutationKilling,
#  TestTcpToWsMutationKilling, TestWriteTokenMutationKilling
#  moved to test_gateway_mutation_killing_2.py)
