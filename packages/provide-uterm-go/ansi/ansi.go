//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package ansi provides ANSI color code conversion for BBS terminal output.
//
// It offers a pluggable dialect registry for converting BBS color tokens to
// standard ANSI escape sequences, plus color-upgrade utilities (16-color →
// 256-color / truecolor).
//
// Built-in dialects: extended tokens ({F#}/{B#}/{P#}/{T#}), TWGS brace tokens
// ({+c}/{-x}/{+Bw}/{NK}), tilde codes (~N), and pipe codes (|00-|23).
// Additional dialects can be registered at runtime via RegisterColorDialect.
package ansi

// RGB is a red/green/blue color triple with components in the 0-255 range.
type RGB struct {
	R, G, B int
}

// DefaultPalette holds the 256-color palette indices that map the 16 base
// BBS colors.
var DefaultPalette = []int{
	0,   // black
	160, // red
	34,  // green
	184, // yellow/brown
	27,  // blue
	127, // magenta
	37,  // cyan
	252, // white
	244, // bright black / gray
	196, // bright red
	46,  // bright green
	226, // bright yellow
	51,  // bright blue
	201, // bright magenta
	87,  // bright cyan
	231, // bright white
}

// DefaultRGB holds direct RGB values for the 16 base BBS colors
// (truecolor output).
var DefaultRGB = []RGB{
	{0, 0, 0},       // black
	{215, 0, 0},     // red
	{0, 175, 0},     // green
	{215, 175, 0},   // yellow/brown
	{0, 95, 255},    // blue
	{175, 0, 175},   // magenta
	{0, 175, 175},   // cyan
	{208, 208, 208}, // white
	{128, 128, 128}, // bright black / gray
	{255, 0, 0},     // bright red
	{0, 255, 0},     // bright green
	{255, 255, 0},   // bright yellow
	{0, 175, 255},   // bright blue
	{255, 0, 255},   // bright magenta
	{95, 255, 255},  // bright cyan
	{255, 255, 255}, // bright white
}

// Common ANSI sequences.
const (
	// ClearScreen clears the screen and homes the cursor.
	ClearScreen = "\x1b[2J\x1b[H"
	// Bold enables bold text.
	Bold = "\x1b[1m"
	// Reset resets all SGR attributes.
	Reset = "\x1b[0m"
)

// cubeLevels are the channel intensities of the xterm 6x6x6 color cube.
var cubeLevels = [6]int{0, 95, 135, 175, 215, 255}

// color256ToRGB converts a 256-color index to an RGB triple.
func color256ToRGB(idx int) RGB {
	if idx < 16 {
		return DefaultRGB[idx]
	}
	if idx < 232 {
		idx -= 16
		b := idx % 6
		idx /= 6
		g := idx % 6
		r := idx / 6
		return RGB{cubeLevels[r], cubeLevels[g], cubeLevels[b]}
	}
	gray := 8 + (idx-232)*10
	return RGB{gray, gray, gray}
}

// paletteToRGB expands a 16-entry 256-color palette into RGB triples.
func paletteToRGB(palette []int) []RGB {
	out := make([]RGB, len(palette))
	for i, idx := range palette {
		out[i] = color256ToRGB(idx)
	}
	return out
}

// mapIndex maps a 16-color SGR code (30-37/90-97 foreground, 40-47/100-107
// background) to a BBS palette index 0-15. The second return value is false
// when the code is not a 16-color SGR color code.
func mapIndex(code int) (int, bool) {
	switch {
	case 30 <= code && code <= 37:
		return code - 30, true
	case 90 <= code && code <= 97:
		return 8 + (code - 90), true
	case 40 <= code && code <= 47:
		return code - 40, true
	case 100 <= code && code <= 107:
		return 8 + (code - 100), true
	}
	return 0, false
}
