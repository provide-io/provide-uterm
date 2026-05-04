#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage suite for the color-downgrade subpackage (100% target)."""

from __future__ import annotations

import pytest

from provide.terminal.colors import (
    apply_color_mode,
    downgrade_to_16,
    downgrade_to_256,
    rgb_to_16_index,
    rgb_to_256,
)
from provide.terminal.colors.rgb import _PALETTE_16, _clamp8
from provide.terminal.colors.sgr import SGR_RE, rewrite_params

pytestmark = pytest.mark.unit


# ── rgb._clamp8 ───────────────────────────────────────────────────────────────


class TestClamp8:
    def test_negative_clamps_to_zero(self):
        assert _clamp8(-1) == 0
        assert _clamp8(-1000) == 0

    def test_zero_passthrough(self):
        assert _clamp8(0) == 0

    def test_midrange_passthrough(self):
        assert _clamp8(128) == 128

    def test_255_passthrough(self):
        assert _clamp8(255) == 255

    def test_over_255_clamps_to_255(self):
        assert _clamp8(256) == 255
        assert _clamp8(99999) == 255


# ── rgb.rgb_to_256 ────────────────────────────────────────────────────────────


class TestRgbTo256:
    def test_pure_black_returns_16(self):
        """r=g=b=0 → r < 8 branch → index 16."""
        assert rgb_to_256(0, 0, 0) == 16

    def test_low_grey_returns_16(self):
        """r=g=b=7 → r < 8 branch → index 16."""
        assert rgb_to_256(7, 7, 7) == 16

    def test_grey_8_uses_ramp(self):
        """r=g=b=8 → grayscale ramp, not 16. Boundary exactly at threshold."""
        assert rgb_to_256(8, 8, 8) == 232

    def test_grey_248_uses_ramp(self):
        """r=g=b=248 → ramp: 232 + int((248-8)/247*24) = 255."""
        assert rgb_to_256(248, 248, 248) == 255

    def test_grey_249_returns_231(self):
        """r=g=b=249 → r > 248 branch → index 231 (bright white cube)."""
        assert rgb_to_256(249, 249, 249) == 231

    def test_grey_255_returns_231(self):
        assert rgb_to_256(255, 255, 255) == 231

    def test_pure_red_cube(self):
        """(255,0,0) → 16 + 36*5 + 6*0 + 0 = 196."""
        assert rgb_to_256(255, 0, 0) == 196

    def test_pure_green_cube(self):
        """(0,255,0) → 16 + 0 + 6*5 + 0 = 46."""
        assert rgb_to_256(0, 255, 0) == 46

    def test_pure_blue_cube(self):
        """(0,0,255) → 16 + 0 + 0 + 5 = 21."""
        assert rgb_to_256(0, 0, 255) == 21

    def test_clamp_out_of_range_inputs(self):
        """Values outside 0-255 are clamped before mapping."""
        # 300 clamps to 255 → rc=5
        assert rgb_to_256(300, 0, 0) == 196
        # -1 clamps to 0 → rc=0
        assert rgb_to_256(-1, 0, 0) == 16  # (0,0,0) → grayscale branch → 16

    def test_midrange_non_grey(self):
        """(128,64,200) exercises the cube with mixed components."""
        result = rgb_to_256(128, 64, 200)
        # rc = round(128/255 * 5) = round(2.51) = 3
        # gc = round(64/255 * 5)  = round(1.25) = 1
        # bc = round(200/255 * 5) = round(3.92) = 4
        # 16 + 36*3 + 6*1 + 4 = 16 + 108 + 6 + 4 = 134
        assert result == 134


# ── rgb.rgb_to_16_index ───────────────────────────────────────────────────────


class TestRgbTo16Index:
    def test_exact_palette_matches(self):
        """Every palette entry should map to its own index."""
        for expected_idx, (r, g, b) in enumerate(_PALETTE_16):
            assert rgb_to_16_index(r, g, b) == expected_idx, f"palette[{expected_idx}]={r, g, b} didn't round-trip"

    def test_bright_white_exact(self):
        assert rgb_to_16_index(255, 255, 255) == 15

    def test_bright_red_exact(self):
        assert rgb_to_16_index(255, 92, 92) == 12

    def test_dark_colors_near_palette(self):
        """Off-palette colors pick the nearest index."""
        # Near black
        assert rgb_to_16_index(1, 1, 1) == 0
        # Near pure red palette entry (205, 0, 0)
        assert rgb_to_16_index(200, 5, 5) == 4

    def test_clamp_out_of_range_inputs(self):
        """Out-of-range inputs are clamped before mapping."""
        assert rgb_to_16_index(400, 400, 400) == 15  # clamps to (255,255,255)
        assert rgb_to_16_index(-100, -100, -100) == 0  # clamps to (0,0,0)


# ── sgr.rewrite_params ────────────────────────────────────────────────────────


class TestRewriteParams:
    def test_empty_params_returns_empty_sgr(self):
        """\x1b[m passes through unchanged (CSI m with no params = reset)."""
        assert rewrite_params("", "256") == "\x1b[m"
        assert rewrite_params("", "16") == "\x1b[m"

    def test_reset_sgr_passes_through(self):
        assert rewrite_params("0", "256") == "\x1b[0m"

    def test_bold_sgr_passes_through(self):
        assert rewrite_params("1", "16") == "\x1b[1m"

    def test_truecolor_fg_to_256(self):
        # 38;2;255;0;0 → 38;5;196
        assert rewrite_params("38;2;255;0;0", "256") == "\x1b[38;5;196m"

    def test_truecolor_bg_to_256(self):
        assert rewrite_params("48;2;0;255;0", "256") == "\x1b[48;5;46m"

    def test_truecolor_fg_to_16(self):
        # (255,92,92) is palette[12] → FG code 91
        assert rewrite_params("38;2;255;92;92", "16") == "\x1b[91m"

    def test_truecolor_bg_to_16(self):
        # (92,92,255) is palette[9] → BG code 104
        assert rewrite_params("48;2;92;92;255", "16") == "\x1b[104m"

    def test_already_256_passes_through(self):
        """38;5;42 is already 256-color, not truecolor — should pass through."""
        assert rewrite_params("38;5;42", "256") == "\x1b[38;5;42m"
        assert rewrite_params("38;5;42", "16") == "\x1b[38;5;42m"

    def test_mixed_params_preserved(self):
        """Non-truecolor params in the same SGR list are preserved in order."""
        # bold + truecolor FG red
        out = rewrite_params("1;38;2;255;0;0", "256")
        assert out == "\x1b[1;38;5;196m"

    def test_truncated_truecolor_run_preserved(self):
        """A 38;2 run with fewer than 5 params is not truecolor; pass through."""
        # 38;2;255 is truncated — don't treat as truecolor
        assert rewrite_params("38;2;255", "256") == "\x1b[38;2;255m"

    def test_non_digit_params_preserved(self):
        """38;2 followed by a non-digit is preserved as-is."""
        assert rewrite_params("38;2;x;y;z", "256") == "\x1b[38;2;x;y;zm"

    def test_multiple_truecolor_runs(self):
        """Multiple truecolor runs in one SGR get rewritten independently."""
        # bold + fg(255,0,0) + bg(0,255,0)
        out = rewrite_params("1;38;2;255;0;0;48;2;0;255;0", "256")
        assert out == "\x1b[1;38;5;196;48;5;46m"


# ── downgrade.downgrade_to_256 / downgrade_to_16 ──────────────────────────────


class TestDowngrade:
    def test_downgrade_to_256_basic(self):
        src = "\x1b[38;2;255;0;0mhello\x1b[0m"
        assert downgrade_to_256(src) == "\x1b[38;5;196mhello\x1b[0m"

    def test_downgrade_to_16_basic(self):
        src = "\x1b[38;2;255;92;92mhello\x1b[0m"
        assert downgrade_to_16(src) == "\x1b[91mhello\x1b[0m"

    def test_downgrade_to_256_idempotent_on_already_256(self):
        already = "\x1b[38;5;42mhello\x1b[0m"
        assert downgrade_to_256(already) == already

    def test_downgrade_to_16_idempotent_on_already_16(self):
        already = "\x1b[31mhello\x1b[0m"
        assert downgrade_to_16(already) == already

    def test_downgrade_leaves_plain_text_alone(self):
        assert downgrade_to_256("no colors here") == "no colors here"
        assert downgrade_to_16("no colors here") == "no colors here"

    def test_downgrade_to_256_multiple_sequences(self):
        src = "\x1b[38;2;255;0;0ma\x1b[0mb\x1b[38;2;0;255;0mc\x1b[0m"
        out = downgrade_to_256(src)
        assert out == "\x1b[38;5;196ma\x1b[0mb\x1b[38;5;46mc\x1b[0m"


# ── mode.apply_color_mode ─────────────────────────────────────────────────────


class TestApplyColorMode:
    def test_passthrough_str(self):
        src = "\x1b[38;2;255;0;0mhello\x1b[0m"
        assert apply_color_mode(src, "passthrough") == src

    def test_passthrough_bytes(self):
        src = b"\x1b[38;2;255;0;0mhello\x1b[0m"
        assert apply_color_mode(src, "passthrough") == src

    def test_str_256(self):
        src = "\x1b[38;2;255;0;0mhello\x1b[0m"
        out = apply_color_mode(src, "256")
        assert out == "\x1b[38;5;196mhello\x1b[0m"
        assert isinstance(out, str)

    def test_str_16(self):
        src = "\x1b[38;2;255;92;92mhello\x1b[0m"
        out = apply_color_mode(src, "16")
        assert out == "\x1b[91mhello\x1b[0m"
        assert isinstance(out, str)

    def test_bytes_256_roundtrips_latin1(self):
        src = b"\x1b[38;2;255;0;0mhello\x1b[0m"
        out = apply_color_mode(src, "256")
        assert out == b"\x1b[38;5;196mhello\x1b[0m"
        assert isinstance(out, bytes)

    def test_bytes_16_roundtrips_latin1(self):
        src = b"\x1b[38;2;255;92;92mhello\x1b[0m"
        out = apply_color_mode(src, "16")
        assert out == b"\x1b[91mhello\x1b[0m"
        assert isinstance(out, bytes)

    def test_bytes_with_non_ascii_bytes_preserved(self):
        """Latin-1 decode/encode must round-trip bytes ≥ 0x80."""
        src = b"\xe2\x98\x85\x1b[38;2;255;0;0m!\x1b[0m"
        out = apply_color_mode(src, "256")
        assert out == b"\xe2\x98\x85\x1b[38;5;196m!\x1b[0m"

    def test_empty_input_str(self):
        assert apply_color_mode("", "256") == ""
        assert apply_color_mode("", "16") == ""
        assert apply_color_mode("", "passthrough") == ""

    def test_empty_input_bytes(self):
        assert apply_color_mode(b"", "256") == b""
        assert apply_color_mode(b"", "16") == b""
        assert apply_color_mode(b"", "passthrough") == b""

    def test_plain_input_unchanged(self):
        assert apply_color_mode("plain", "256") == "plain"
        assert apply_color_mode(b"plain", "16") == b"plain"


# ── SGR_RE sanity ─────────────────────────────────────────────────────────────


class TestSgrRegex:
    def test_matches_typical_sgr(self):
        m = SGR_RE.match("\x1b[38;2;255;0;0m")
        assert m is not None
        assert m.group(1) == "38;2;255;0;0"

    def test_matches_empty_params(self):
        m = SGR_RE.match("\x1b[m")
        assert m is not None
        assert m.group(1) == ""

    def test_does_not_match_non_sgr(self):
        """CSI without final 'm' is not an SGR sequence."""
        assert SGR_RE.match("\x1b[2J") is None
