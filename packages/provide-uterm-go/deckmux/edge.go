//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import "math"

// ViewportToEdgeRange converts a viewport scroll position to (topPct,
// heightPct) edge-bar fractions in [0,1], each rounded to 4 decimals.
// Mirrors Python viewport_to_edge_range: a non-positive totalLines yields the
// full-height default (0.0, 1.0).
func ViewportToEdgeRange(scrollTopLine, visibleLines, totalLines int) (topPct, heightPct float64) {
	if totalLines <= 0 {
		return 0.0, 1.0
	}
	top := float64(scrollTopLine) / float64(totalLines)
	height := math.Min(float64(visibleLines)/float64(totalLines), 1.0-top)
	return round4(top), round4(height)
}

// LineToEdgePosition converts a single line number to an edge-bar position in
// [0,1], rounded to 4 decimals. Mirrors Python line_to_edge_position.
func LineToEdgePosition(line, totalLines int) float64 {
	if totalLines <= 0 {
		return 0.0
	}
	return round4(math.Min(float64(line)/float64(totalLines), 1.0))
}

// ScrollCenterLine returns the center line of a viewport (scrollTop +
// visibleLines//2), matching Python scroll_center_line.
func ScrollCenterLine(scrollTop, visibleLines int) int {
	return scrollTop + visibleLines/2
}

// round4 rounds to 4 decimal places. Python's round() uses banker's rounding,
// but none of the DeckMux edge values land on an exact 4th-decimal .5
// tie, so half-away-from-zero is indistinguishable here.
func round4(x float64) float64 {
	return math.Round(x*1e4) / 1e4
}
