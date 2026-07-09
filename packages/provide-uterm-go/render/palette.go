//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package render provides ANSI palette quantizers, SGR emitters, and
// structured text segments for non-terminal clients. Port of
// provide.uterm.render (palette.py, sgr.py, segments.py).
package render

import "sync"

// RGB is a 24-bit color triple.
type RGB struct {
	R, G, B int
}

// ANSI16Entry is one standard 16-color palette entry: its RGB plus the SGR
// foreground and background codes.
type ANSI16Entry struct {
	RGB    RGB
	FGCode int
	BGCode int
}

// ANSI16Palette is the standard 16-color ANSI palette.
var ANSI16Palette = []ANSI16Entry{
	{RGB{0, 0, 0}, 30, 40},
	{RGB{170, 0, 0}, 31, 41},
	{RGB{0, 170, 0}, 32, 42},
	{RGB{170, 85, 0}, 33, 43},
	{RGB{0, 0, 170}, 34, 44},
	{RGB{170, 0, 170}, 35, 45},
	{RGB{0, 170, 170}, 36, 46},
	{RGB{170, 170, 170}, 37, 47},
	{RGB{85, 85, 85}, 90, 100},
	{RGB{255, 85, 85}, 91, 101},
	{RGB{85, 255, 85}, 92, 102},
	{RGB{255, 255, 85}, 93, 103},
	{RGB{85, 85, 255}, 94, 104},
	{RGB{255, 85, 255}, 95, 105},
	{RGB{85, 255, 255}, 96, 106},
	{RGB{255, 255, 255}, 97, 107},
}

var (
	xterm256     []RGB
	xterm256Once sync.Once
)

// buildXterm256 lazily builds the xterm 256-color palette: the standard 16,
// the 216-color cube (16-231), and 24 grays (232-255).
func buildXterm256() {
	xterm256Once.Do(func() {
		xterm256 = make([]RGB, 0, 256)
		for _, e := range ANSI16Palette {
			xterm256 = append(xterm256, e.RGB)
		}
		for ri := range 6 {
			for gi := range 6 {
				for bi := range 6 {
					level := func(i int) int {
						if i == 0 {
							return 0
						}
						return 55 + 40*i
					}
					xterm256 = append(xterm256, RGB{level(ri), level(gi), level(bi)})
				}
			}
		}
		for i := range 24 {
			v := 8 + 10*i
			xterm256 = append(xterm256, RGB{v, v, v})
		}
	})
}

func colorDistSq(a, b RGB) int {
	dr, dg, db := a.R-b.R, a.G-b.G, a.B-b.B
	return dr*dr + dg*dg + db*db
}

// Nearest16 returns the (fgCode, bgCode) pair for the nearest 16-color
// match.
func Nearest16(r, g, b int) (fgCode, bgCode int) {
	target := RGB{r, g, b}
	bestI := 0
	bestD := colorDistSq(target, ANSI16Palette[0].RGB)
	for i := 1; i < 16; i++ {
		if d := colorDistSq(target, ANSI16Palette[i].RGB); d < bestD {
			bestD = d
			bestI = i
		}
	}
	return ANSI16Palette[bestI].FGCode, ANSI16Palette[bestI].BGCode
}

// Nearest256 returns the xterm 256-color index for the nearest match.
func Nearest256(r, g, b int) int {
	buildXterm256()
	target := RGB{r, g, b}
	bestI := 0
	bestD := colorDistSq(target, xterm256[0])
	for i := 1; i < 256; i++ {
		if d := colorDistSq(target, xterm256[i]); d < bestD {
			bestD = d
			bestI = i
		}
	}
	return bestI
}
