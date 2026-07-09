//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ansi

import "testing"

func TestConstants(t *testing.T) {
	if ClearScreen != "\x1b[2J\x1b[H" {
		t.Errorf("ClearScreen = %q", ClearScreen)
	}
	if Bold != "\x1b[1m" {
		t.Errorf("Bold = %q", Bold)
	}
	if Reset != "\x1b[0m" {
		t.Errorf("Reset = %q", Reset)
	}
}

func TestDefaultPaletteLength(t *testing.T) {
	if len(DefaultPalette) != 16 {
		t.Fatalf("len(DefaultPalette) = %d, want 16", len(DefaultPalette))
	}
}

func TestDefaultRGBLength(t *testing.T) {
	if len(DefaultRGB) != 16 {
		t.Fatalf("len(DefaultRGB) = %d, want 16", len(DefaultRGB))
	}
}

func TestDefaultRGBComponentsInRange(t *testing.T) {
	for i, c := range DefaultRGB {
		for _, v := range []int{c.R, c.G, c.B} {
			if v < 0 || v > 255 {
				t.Errorf("DefaultRGB[%d] = %+v has out-of-range component", i, c)
			}
		}
	}
}

func TestColor256ToRGB(t *testing.T) {
	tests := []struct {
		name string
		idx  int
		want RGB
	}{
		{"index 0 is black (DefaultRGB table)", 0, DefaultRGB[0]},
		{"index 15 uses DefaultRGB table", 15, DefaultRGB[15]},
		{"index 16 is first cube colour", 16, RGB{0, 0, 0}},
		{"index 17 levels 1 blue", 17, RGB{0, 0, 95}},
		{"index 18 levels 2 blue", 18, RGB{0, 0, 135}},
		{"index 19 levels 3 blue", 19, RGB{0, 0, 175}},
		{"index 20 levels 4 blue", 20, RGB{0, 0, 215}},
		{"index 21 levels 5 blue", 21, RGB{0, 0, 255}},
		{"index 22 levels 1 green", 22, RGB{0, 95, 0}},
		{"index 88 levels 2 red", 88, RGB{135, 0, 0}},
		{"index 112 levels mixed", 112, RGB{135, 215, 0}},
		{"index 231 is last cube colour", 231, RGB{255, 255, 255}},
		{"index 232 is first greyscale", 232, RGB{8, 8, 8}},
		{"index 255 is last greyscale", 255, RGB{238, 238, 238}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := color256ToRGB(tt.idx); got != tt.want {
				t.Errorf("color256ToRGB(%d) = %+v, want %+v", tt.idx, got, tt.want)
			}
		})
	}
}

func TestPaletteToRGB(t *testing.T) {
	got := paletteToRGB(DefaultPalette)
	if len(got) != 16 {
		t.Fatalf("len = %d, want 16", len(got))
	}
	for i, idx := range DefaultPalette {
		if got[i] != color256ToRGB(idx) {
			t.Errorf("paletteToRGB[%d] = %+v, want %+v", i, got[i], color256ToRGB(idx))
		}
	}
}

func TestMapIndex(t *testing.T) {
	tests := []struct {
		code   int
		want   int
		wantOK bool
	}{
		{29, 0, false},
		{30, 0, true},
		{37, 7, true},
		{38, 0, false},
		{39, 0, false},
		{40, 0, true},
		{47, 7, true},
		{48, 0, false},
		{89, 0, false},
		{90, 8, true},
		{97, 15, true},
		{98, 0, false},
		{99, 0, false},
		{100, 8, true},
		{107, 15, true},
		{108, 0, false},
		{1, 0, false},
		{0, 0, false},
	}
	for _, tt := range tests {
		got, ok := mapIndex(tt.code)
		if got != tt.want || ok != tt.wantOK {
			t.Errorf("mapIndex(%d) = (%d, %v), want (%d, %v)", tt.code, got, ok, tt.want, tt.wantOK)
		}
	}
}
