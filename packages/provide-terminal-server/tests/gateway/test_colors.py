#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway._colors."""

from __future__ import annotations

from provide.terminal.gateway._colors import (
    _apply_color_mode,
    _clamp8,
    _rgb_to_16_index,
    _rgb_to_256,
)

# ---------------------------------------------------------------------------
# _clamp8
# ---------------------------------------------------------------------------


class TestClamp8:
    def test_negative_clamped_to_zero(self) -> None:
        assert _clamp8(-10) == 0

    def test_zero(self) -> None:
        assert _clamp8(0) == 0

    def test_mid(self) -> None:
        assert _clamp8(128) == 128

    def test_255(self) -> None:
        assert _clamp8(255) == 255

    def test_over_255_clamped(self) -> None:
        assert _clamp8(300) == 255


# ---------------------------------------------------------------------------
# _rgb_to_256
# ---------------------------------------------------------------------------


class TestRgbTo256:
    def test_black_grayscale(self) -> None:
        # r==g==b, r < 8 → 16
        assert _rgb_to_256(0, 0, 0) == 16

    def test_near_white_grayscale(self) -> None:
        # r==g==b, r > 248 → 231
        assert _rgb_to_256(255, 255, 255) == 231

    def test_mid_grayscale(self) -> None:
        # r==g==b, 8 <= r <= 248 → grayscale ramp
        result = _rgb_to_256(128, 128, 128)
        assert 232 <= result <= 255

    def test_color_cube(self) -> None:
        # non-gray: maps to 6x6x6 cube
        result = _rgb_to_256(255, 0, 0)
        assert 16 <= result <= 231

    def test_color_cube_green(self) -> None:
        result = _rgb_to_256(0, 255, 0)
        assert 16 <= result <= 231

    def test_color_cube_blue(self) -> None:
        result = _rgb_to_256(0, 0, 255)
        assert 16 <= result <= 231

    def test_grayscale_boundary_8(self) -> None:
        # r==g==b==8 → should hit the grayscale ramp branch
        result = _rgb_to_256(8, 8, 8)
        assert 232 <= result <= 255

    def test_grayscale_boundary_248(self) -> None:
        # r==g==b==248 → should hit the grayscale ramp branch
        result = _rgb_to_256(248, 248, 248)
        assert 232 <= result <= 255


# ---------------------------------------------------------------------------
# _rgb_to_16_index
# ---------------------------------------------------------------------------


class TestRgbTo16Index:
    def test_black(self) -> None:
        assert _rgb_to_16_index(0, 0, 0) == 0

    def test_white(self) -> None:
        assert _rgb_to_16_index(255, 255, 255) == 15

    def test_red(self) -> None:
        result = _rgb_to_16_index(205, 0, 0)
        assert result == 4  # exact match for table entry

    def test_clamped_values(self) -> None:
        # Values out of range get clamped before matching
        result = _rgb_to_16_index(-10, 300, 128)
        assert 0 <= result <= 15


# ---------------------------------------------------------------------------
# _apply_color_mode
# ---------------------------------------------------------------------------


class TestApplyColorMode:
    def test_passthrough_returns_raw(self) -> None:
        raw = b"\x1b[38;2;255;0;0mHello"
        assert _apply_color_mode(raw, "passthrough") == raw

    def test_256_downgrades_truecolor_fg(self) -> None:
        raw = b"\x1b[38;2;255;0;0mRed"
        result = _apply_color_mode(raw, "256")
        # Should become \x1b[38;5;NNNm
        assert b"38;5;" in result
        assert b"38;2;" not in result

    def test_256_downgrades_truecolor_bg(self) -> None:
        raw = b"\x1b[48;2;0;255;0mGreen"
        result = _apply_color_mode(raw, "256")
        assert b"48;5;" in result
        assert b"48;2;" not in result

    def test_16_downgrades_truecolor_fg(self) -> None:
        raw = b"\x1b[38;2;255;0;0mRed"
        result = _apply_color_mode(raw, "16")
        # Should NOT contain 38;2 or 38;5
        assert b"38;2;" not in result
        assert b"38;5;" not in result

    def test_16_downgrades_truecolor_bg(self) -> None:
        raw = b"\x1b[48;2;0;255;0mGreen"
        result = _apply_color_mode(raw, "16")
        assert b"48;2;" not in result
        assert b"48;5;" not in result

    def test_16_fg_red_maps_to_sgr_31(self) -> None:
        """Exact red (205,0,0) → fg red = \x1b[31m."""
        raw = b"\x1b[38;2;205;0;0mRed"
        assert _apply_color_mode(raw, "16") == b"\x1b[31mRed"

    def test_16_bg_red_maps_to_sgr_41(self) -> None:
        """Exact red background → bg red = \x1b[41m."""
        raw = b"\x1b[48;2;205;0;0mRed"
        assert _apply_color_mode(raw, "16") == b"\x1b[41mRed"

    def test_16_fg_white_maps_to_sgr_37(self) -> None:
        """Near-white (229,229,229) → fg white = \x1b[37m."""
        raw = b"\x1b[38;2;229;229;229mWhite"
        assert _apply_color_mode(raw, "16") == b"\x1b[37mWhite"

    def test_16_fg_bright_white_maps_to_sgr_97(self) -> None:
        """Pure white (255,255,255) → fg bright white = \x1b[97m."""
        raw = b"\x1b[38;2;255;255;255mBrightWhite"
        assert _apply_color_mode(raw, "16") == b"\x1b[97mBrightWhite"

    def test_16_fg_black_maps_to_sgr_30(self) -> None:
        """Pure black (0,0,0) → fg black = \x1b[30m."""
        raw = b"\x1b[38;2;0;0;0mBlack"
        assert _apply_color_mode(raw, "16") == b"\x1b[30mBlack"

    def test_16_fg_cyan_maps_to_sgr_36(self) -> None:
        """Exact cyan (0,205,205) → fg cyan = \x1b[36m."""
        raw = b"\x1b[38;2;0;205;205mCyan"
        assert _apply_color_mode(raw, "16") == b"\x1b[36mCyan"

    def test_16_fg_bright_red_maps_to_sgr_91(self) -> None:
        """Bright red (255,92,92) → fg bright red = \x1b[91m."""
        raw = b"\x1b[38;2;255;92;92mBrightRed"
        assert _apply_color_mode(raw, "16") == b"\x1b[91mBrightRed"

    def test_256_passthrough_in_16_mode(self) -> None:
        """256-color codes pass through unchanged in 16 mode (no 256→16 conversion)."""
        raw = b"\x1b[38;5;196mRed256"
        assert _apply_color_mode(raw, "16") == raw

    def test_non_color_sgr_passes_through(self) -> None:
        # Bold + reset are left alone
        raw = b"\x1b[1mBold\x1b[0mPlain"
        assert _apply_color_mode(raw, "256") == raw
        assert _apply_color_mode(raw, "16") == raw

    def test_empty_params_sgr_passes_through(self) -> None:
        # \x1b[m  (empty params)
        raw = b"\x1b[m"
        assert _apply_color_mode(raw, "256") == raw

    def test_mixed_params_with_truecolor(self) -> None:
        # Bold + truecolor fg: \x1b[1;38;2;100;200;50m
        raw = b"\x1b[1;38;2;100;200;50m"
        result = _apply_color_mode(raw, "256")
        assert b"38;5;" in result
        # "1" (bold) should still be present
        assert result.startswith(b"\x1b[1;38;5;")

    def test_non_truecolor_params_passed_unchanged(self) -> None:
        # 38 followed by something other than 2 → not truecolor
        raw = b"\x1b[38;5;196m"
        assert _apply_color_mode(raw, "256") == raw

    def test_truncated_truecolor_not_enough_parts(self) -> None:
        # 38;2 with only 2 numbers (needs 3) → not matched as truecolor
        raw = b"\x1b[38;2;100;200m"
        assert _apply_color_mode(raw, "256") == raw

    def test_non_digit_in_rgb_position(self) -> None:
        # non-digit in what would be the RGB position → not matched
        raw = b"\x1b[38;2;abc;200;50m"
        assert _apply_color_mode(raw, "256") == raw
