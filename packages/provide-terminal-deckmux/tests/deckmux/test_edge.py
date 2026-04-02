#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.deckmux._edge — viewport range calculation."""

from __future__ import annotations

from provide.terminal.deckmux._edge import (
    line_to_edge_position,
    scroll_center_line,
    viewport_to_edge_range,
)

# --- viewport_to_edge_range ---


def test_viewport_to_edge_range_normal() -> None:
    top, height = viewport_to_edge_range(0, 24, 100)
    assert top == 0.0
    assert height == 0.24


def test_viewport_to_edge_range_scrolled() -> None:
    top, height = viewport_to_edge_range(50, 24, 100)
    assert top == 0.5
    assert height == 0.24


def test_viewport_to_edge_range_at_bottom() -> None:
    top, height = viewport_to_edge_range(76, 24, 100)
    assert top == 0.76
    assert height == 0.24


def test_viewport_to_edge_range_past_end() -> None:
    """Height clamped so top + height <= 1.0."""
    top, height = viewport_to_edge_range(90, 24, 100)
    assert top == 0.9
    assert height == 0.1  # clamped to 1.0 - 0.9


def test_viewport_to_edge_range_zero_total() -> None:
    top, height = viewport_to_edge_range(0, 24, 0)
    assert top == 0.0
    assert height == 1.0


def test_viewport_to_edge_range_negative_total() -> None:
    top, height = viewport_to_edge_range(0, 24, -5)
    assert top == 0.0
    assert height == 1.0


def test_viewport_to_edge_range_full_viewport() -> None:
    """Viewport covers all lines."""
    top, height = viewport_to_edge_range(0, 100, 100)
    assert top == 0.0
    assert height == 1.0


def test_viewport_to_edge_range_viewport_larger_than_total() -> None:
    top, height = viewport_to_edge_range(0, 200, 100)
    assert top == 0.0
    assert height == 1.0  # clamped


# --- line_to_edge_position ---


def test_line_to_edge_position_start() -> None:
    assert line_to_edge_position(0, 100) == 0.0


def test_line_to_edge_position_middle() -> None:
    assert line_to_edge_position(50, 100) == 0.5


def test_line_to_edge_position_end() -> None:
    assert line_to_edge_position(100, 100) == 1.0


def test_line_to_edge_position_beyond_end() -> None:
    assert line_to_edge_position(150, 100) == 1.0


def test_line_to_edge_position_zero_total() -> None:
    assert line_to_edge_position(0, 0) == 0.0


def test_line_to_edge_position_negative_total() -> None:
    assert line_to_edge_position(10, -5) == 0.0


def test_line_to_edge_position_total_one_nonzero_line() -> None:
    """total_lines=1 is valid; line=1 should return 1.0, not fall through guard."""
    assert line_to_edge_position(1, 1) == 1.0


def test_line_to_edge_position_rounds_to_4_decimal_places() -> None:
    """Result is rounded to exactly 4 decimal places."""
    result = line_to_edge_position(1, 3)  # 1/3 = 0.3333...
    assert result == round(1 / 3, 4)
    assert len(str(result).rstrip("0").split(".")[-1]) <= 4


def test_viewport_to_edge_range_total_one_nonzero_top() -> None:
    """total_lines=1 with scroll_top=1 → top=1.0, not (0.0, 1.0) from guard."""
    top, height = viewport_to_edge_range(1, 24, 1)
    assert top == 1.0
    assert height == 0.0


def test_viewport_to_edge_range_rounds_top_to_4_decimal_places() -> None:
    """top_pct is rounded to exactly 4 decimal places, not 5."""
    top, _ = viewport_to_edge_range(1, 24, 3)  # 1/3 = 0.3333...
    assert top == round(1 / 3, 4)
    assert top != round(1 / 3, 5)


def test_viewport_to_edge_range_rounds_height_to_4_decimal_places() -> None:
    """height_pct is rounded to exactly 4 decimal places, not 5."""
    _, height = viewport_to_edge_range(0, 1, 3)  # 1/3 = 0.3333...
    assert height == round(1 / 3, 4)
    assert height != round(1 / 3, 5)


# --- scroll_center_line ---


def test_scroll_center_line() -> None:
    assert scroll_center_line(0, 24) == 12


def test_scroll_center_line_offset() -> None:
    assert scroll_center_line(50, 24) == 62


def test_scroll_center_line_odd_visible() -> None:
    assert scroll_center_line(10, 25) == 22  # 10 + 12
