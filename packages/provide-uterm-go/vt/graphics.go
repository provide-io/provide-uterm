//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "fmt"

// SGR lookup tables, mirroring pyte.graphics.

// fgANSI maps ANSI foreground color codes to color names.
var fgANSI = map[int]string{
	30: "black",
	31: "red",
	32: "green",
	33: "brown",
	34: "blue",
	35: "magenta",
	36: "cyan",
	37: "white",
	39: "default",
}

// bgANSI maps ANSI background color codes to color names.
var bgANSI = map[int]string{
	40: "black",
	41: "red",
	42: "green",
	43: "brown",
	44: "blue",
	45: "magenta",
	46: "cyan",
	47: "white",
	49: "default",
}

// fgAIXTerm maps aixterm high-intensity foreground codes to color names.
var fgAIXTerm = map[int]string{
	90: "brightblack",
	91: "brightred",
	92: "brightgreen",
	93: "brightbrown",
	94: "brightblue",
	95: "brightmagenta",
	96: "brightcyan",
	97: "brightwhite",
}

// bgAIXTerm maps aixterm high-intensity background codes to color names.
// The "bfightmagenta" value reproduces a typo in pyte.graphics.BG_AIXTERM;
// it is intentional so wire-level state matches pyte exactly.
var bgAIXTerm = map[int]string{
	100: "brightblack",
	101: "brightred",
	102: "brightgreen",
	103: "brightbrown",
	104: "brightblue",
	105: "bfightmagenta",
	106: "brightcyan",
	107: "brightwhite",
}

// fgBG256 is the 256-color palette as lowercase rrggbb hex strings,
// computed with the same formula as pyte.graphics.FG_BG_256.
var fgBG256 = makeFGBG256()

func makeFGBG256() []string {
	rgb := [][3]int{
		{0x00, 0x00, 0x00}, {0xcd, 0x00, 0x00}, {0x00, 0xcd, 0x00}, {0xcd, 0xcd, 0x00},
		{0x00, 0x00, 0xee}, {0xcd, 0x00, 0xcd}, {0x00, 0xcd, 0xcd}, {0xe5, 0xe5, 0xe5},
		{0x7f, 0x7f, 0x7f}, {0xff, 0x00, 0x00}, {0x00, 0xff, 0x00}, {0xff, 0xff, 0x00},
		{0x5c, 0x5c, 0xff}, {0xff, 0x00, 0xff}, {0x00, 0xff, 0xff}, {0xff, 0xff, 0xff},
	}

	// Colors 16..231: the 6x6x6 color cube.
	steps := [6]int{0x00, 0x5f, 0x87, 0xaf, 0xd7, 0xff}
	for i := 0; i < 216; i++ {
		rgb = append(rgb, [3]int{
			steps[(i/36)%6],
			steps[(i/6)%6],
			steps[i%6],
		})
	}

	// Colors 232..255: grayscale.
	for i := 0; i < 24; i++ {
		v := 8 + i*10
		rgb = append(rgb, [3]int{v, v, v})
	}

	out := make([]string, len(rgb))
	for i, c := range rgb {
		out[i] = fmt.Sprintf("%02x%02x%02x", c[0], c[1], c[2])
	}
	return out
}
