//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package colors provides color-downgrade utilities: truecolor SGR →
// 256-color / 16-color.
//
// These are the downgrade counterparts to UpgradeTo256 / UpgradeToTruecolor
// in the ansi package. Each concern lives in its own tight file:
//
//   - rgb.go       — RGB-to-palette-index mapping (RGBTo256, RGBTo16Index).
//   - sgr.go       — SGR parameter-list rewriting.
//   - downgrade.go — text-level DowngradeTo256 / DowngradeTo16.
//   - mode.go      — unified ApplyColorMode / ApplyColorModeBytes dispatchers.
package colors

import "math"

// palette16 is the BBS-canonical 16-color reference palette. Values match
// typical xterm / PuTTY defaults (index 0 = black, 15 = bright white).
// Euclidean distance in RGB space is used to find the nearest index.
var palette16 = [16][3]int{
	{0, 0, 0},
	{0, 0, 205},
	{0, 205, 0},
	{0, 205, 205},
	{205, 0, 0},
	{205, 0, 205},
	{205, 205, 0},
	{229, 229, 229},
	{127, 127, 127},
	{92, 92, 255},
	{92, 255, 92},
	{92, 255, 255},
	{255, 92, 92},
	{255, 92, 255},
	{255, 255, 92},
	{255, 255, 255},
}

// clamp8 clamps an integer to the 0-255 range.
func clamp8(v int) int {
	if v < 0 {
		return 0
	}
	if v > 255 {
		return 255
	}
	return v
}

// RGBTo256 maps an (R, G, B) triple to the nearest xterm-256 palette index.
//
// It uses the standard 6x6x6 color cube (indices 16-231) plus the 24-step
// greyscale ramp (indices 232-255). When R == G == B the greyscale ramp is
// preferred for finer luminance resolution.
//
// Components are clamped to 0-255; the result is in the 16-255 range.
func RGBTo256(r, g, b int) int {
	rr, gg, bb := clamp8(r), clamp8(g), clamp8(b)
	if rr == gg && gg == bb {
		if rr < 8 {
			return 16
		}
		if rr > 248 {
			return 231
		}
		return 232 + int(float64(rr-8)/247*24)
	}
	// math.RoundToEven mirrors Python's round() (banker's rounding).
	rc := int(math.RoundToEven(float64(rr) / 255 * 5))
	gc := int(math.RoundToEven(float64(gg) / 255 * 5))
	bc := int(math.RoundToEven(float64(bb) / 255 * 5))
	return 16 + 36*rc + 6*gc + bc
}

// RGBTo16Index maps an (R, G, B) triple to the nearest base-16 ANSI palette
// index.
//
// It uses Euclidean distance in RGB space against the reference palette
// (xterm/PuTTY defaults) and returns an index 0-15 where 0-7 are the normal
// colors and 8-15 are the bright variants. Components are clamped to 0-255.
func RGBTo16Index(r, g, b int) int {
	rr, gg, bb := clamp8(r), clamp8(g), clamp8(b)
	bestI, bestD := 0, int(1e9)
	for i, t := range palette16 {
		tr, tg, tb := t[0], t[1], t[2]
		d := (rr-tr)*(rr-tr) + (gg-tg)*(gg-tg) + (bb-tb)*(bb-tb)
		if d < bestD {
			bestD = d
			bestI = i
		}
	}
	return bestI
}
