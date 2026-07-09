//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import "fmt"

// ColorMode selects an SGR emitter: "truecolor", "256", or "16".
type ColorMode string

// Color modes.
const (
	ModeTruecolor ColorMode = "truecolor"
	Mode256       ColorMode = "256"
	Mode16        ColorMode = "16"
)

// SGRTruecolor emits a truecolor (24-bit) SGR sequence for fg over bg.
func SGRTruecolor(fg, bg RGB) string {
	return fmt.Sprintf("\x1b[38;2;%d;%d;%d;48;2;%d;%d;%dm", fg.R, fg.G, fg.B, bg.R, bg.G, bg.B)
}

// SGR256 emits a 256-color SGR sequence for fg over bg.
func SGR256(fg, bg RGB) string {
	fi := Nearest256(fg.R, fg.G, fg.B)
	bi := Nearest256(bg.R, bg.G, bg.B)
	return fmt.Sprintf("\x1b[38;5;%d;48;5;%dm", fi, bi)
}

// SGR16 emits a 16-color SGR sequence for fg over bg.
func SGR16(fg, bg RGB) string {
	fgCode, _ := Nearest16(fg.R, fg.G, fg.B)
	_, bgCode := Nearest16(bg.R, bg.G, bg.B)
	return fmt.Sprintf("\x1b[%d;%dm", fgCode, bgCode)
}

// SGRFunc emits an SGR sequence for a fg/bg pair.
type SGRFunc func(fg, bg RGB) string

// SGRFunctions maps each ColorMode to its emitter.
var SGRFunctions = map[ColorMode]SGRFunc{
	ModeTruecolor: SGRTruecolor,
	Mode256:       SGR256,
	Mode16:        SGR16,
}
