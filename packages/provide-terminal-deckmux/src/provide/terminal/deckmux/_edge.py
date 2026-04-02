#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux edge indicators — viewport range calculation."""

from __future__ import annotations


def viewport_to_edge_range(
    scroll_top_line: int,
    visible_lines: int,
    total_lines: int,
) -> tuple[float, float]:
    """Convert viewport scroll position to edge bar percentages.

    Returns (top_pct, height_pct) as 0.0-1.0 values for positioning
    the edge indicator bar.
    """
    if total_lines <= 0:
        return 0.0, 1.0
    top_pct = scroll_top_line / total_lines
    height_pct = min(visible_lines / total_lines, 1.0 - top_pct)
    return round(top_pct, 4), round(height_pct, 4)


def line_to_edge_position(line: int, total_lines: int) -> float:
    """Convert a single line number to an edge bar position (0.0-1.0)."""
    if total_lines <= 0:
        return 0.0
    return round(min(line / total_lines, 1.0), 4)


def scroll_center_line(scroll_top: int, visible_lines: int) -> int:
    """Calculate the center line of a viewport."""
    return scroll_top + visible_lines // 2
