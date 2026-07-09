//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"math"
	"testing"
)

func TestViewportToEdgeRange(t *testing.T) {
	cases := []struct {
		top, vis, total     int
		wantTop, wantHeight float64
	}{
		{0, 24, 100, 0.0, 0.24},
		{50, 24, 100, 0.5, 0.24},
		{76, 24, 100, 0.76, 0.24},
		{90, 24, 100, 0.9, 0.1}, // clamped to 1.0 - 0.9
		{0, 24, 0, 0.0, 1.0},    // zero total
		{0, 24, -5, 0.0, 1.0},   // negative total
		{0, 100, 100, 0.0, 1.0}, // full viewport
		{0, 200, 100, 0.0, 1.0}, // viewport larger than total (clamped)
		{1, 24, 1, 1.0, 0.0},    // total_lines=1, scroll_top=1
	}
	for _, c := range cases {
		top, height := ViewportToEdgeRange(c.top, c.vis, c.total)
		if top != c.wantTop || height != c.wantHeight {
			t.Errorf("ViewportToEdgeRange(%d,%d,%d) = (%v,%v), want (%v,%v)",
				c.top, c.vis, c.total, top, height, c.wantTop, c.wantHeight)
		}
	}
}

func TestViewportToEdgeRangeRounding(t *testing.T) {
	top, _ := ViewportToEdgeRange(1, 24, 3) // 1/3
	if top != round4(1.0/3.0) || top == round5(1.0/3.0) {
		t.Errorf("top rounding = %v", top)
	}
	_, height := ViewportToEdgeRange(0, 1, 3) // 1/3
	if height != round4(1.0/3.0) || height == round5(1.0/3.0) {
		t.Errorf("height rounding = %v", height)
	}
}

func TestLineToEdgePosition(t *testing.T) {
	cases := []struct {
		line, total int
		want        float64
	}{
		{0, 100, 0.0},
		{50, 100, 0.5},
		{100, 100, 1.0},
		{150, 100, 1.0}, // beyond end clamped
		{0, 0, 0.0},     // zero total
		{10, -5, 0.0},   // negative total
		{1, 1, 1.0},     // total=1
	}
	for _, c := range cases {
		if got := LineToEdgePosition(c.line, c.total); got != c.want {
			t.Errorf("LineToEdgePosition(%d,%d) = %v, want %v", c.line, c.total, got, c.want)
		}
	}
}

func TestLineToEdgePositionRounding(t *testing.T) {
	if got := LineToEdgePosition(1, 3); got != round4(1.0/3.0) {
		t.Errorf("= %v", got)
	}
}

func TestScrollCenterLine(t *testing.T) {
	cases := []struct {
		top, vis, want int
	}{
		{0, 24, 12},
		{50, 24, 62},
		{10, 25, 22},
	}
	for _, c := range cases {
		if got := ScrollCenterLine(c.top, c.vis); got != c.want {
			t.Errorf("ScrollCenterLine(%d,%d) = %d, want %d", c.top, c.vis, got, c.want)
		}
	}
}

func round5(x float64) float64 { return math.Round(x*1e5) / 1e5 }
