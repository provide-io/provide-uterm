//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vt"
)

// ANSI escape constants (port of render/buffer.py).
const (
	ANSIReset      = "\x1b[0m"
	ANSIHideCursor = "\x1b[?25l"
	ANSIShowCursor = "\x1b[?25h"
	ANSIAltScreen  = "\x1b[?1049h"
	ANSIExitAlt    = "\x1b[?1049l"
)

// MoveTo returns the CSI sequence to position the cursor at row, col
// (1-based).
func MoveTo(row, col int) string {
	return fmt.Sprintf("\x1b[%d;%dH", row, col)
}

// ClearScreen returns the CSI sequence to erase the entire screen.
func ClearScreen() string {
	return "\x1b[2J"
}

// FGCodes maps 16-color names to SGR foreground codes.
var FGCodes = map[string]int{
	"black": 30, "red": 31, "green": 32, "yellow": 33,
	"blue": 34, "magenta": 35, "cyan": 36, "white": 37,
	"brightblack": 90, "brightred": 91, "brightgreen": 92, "brightyellow": 93,
	"brightblue": 94, "brightmagenta": 95, "brightcyan": 96, "brightwhite": 97,
}

// BGCodes maps 16-color names to SGR background codes.
var BGCodes = map[string]int{
	"black": 40, "red": 41, "green": 42, "yellow": 43,
	"blue": 44, "magenta": 45, "cyan": 46, "white": 47,
	"brightblack": 100, "brightred": 101, "brightgreen": 102, "brightyellow": 103,
	"brightblue": 104, "brightmagenta": 105, "brightcyan": 106, "brightwhite": 107,
}

func isHexColor(value string) bool {
	if len(value) != 6 {
		return false
	}
	for _, c := range strings.ToLower(value) {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func hexToRGB(value string) (int, int, int) {
	r, _ := strconv.ParseInt(value[0:2], 16, 32)
	g, _ := strconv.ParseInt(value[2:4], 16, 32)
	b, _ := strconv.ParseInt(value[4:6], 16, 32)
	return int(r), int(g), int(b)
}

// colorSGR returns SGR codes for a pyte-style color value: "default" → none,
// a named 16-color → one code, a 6-char hex → truecolor operands.
func colorSGR(color string, isFG bool) []int {
	if color == "default" {
		return nil
	}
	table := FGCodes
	base := 38
	if !isFG {
		table = BGCodes
		base = 48
	}
	if code, ok := table[color]; ok {
		return []int{code}
	}
	if isHexColor(color) {
		r, g, b := hexToRGB(color)
		return []int{base, 2, r, g, b}
	}
	return nil
}

// Style is a pyte cell's render-relevant attributes.
type Style struct {
	FG, BG     string
	Bold       bool
	Underscore bool
	Reverse    bool
	Blink      bool
}

// DefaultStyle is the style of an absent cell.
var DefaultStyle = Style{FG: "default", BG: "default"}

// StyleToSGR converts pyte cell style attributes to an SGR escape sequence.
// Supports named 16-color, and pyte hex values as truecolor.
func StyleToSGR(s Style) string {
	fg, bg := s.FG, s.BG
	if s.Reverse {
		fg, bg = bg, fg
	}
	var codes []int
	if s.Bold {
		codes = append(codes, 1)
	}
	if s.Underscore {
		codes = append(codes, 4)
	}
	if s.Blink {
		codes = append(codes, 5)
	}
	codes = append(codes, colorSGR(fg, true)...)
	codes = append(codes, colorSGR(bg, false)...)
	if len(codes) == 0 {
		return ANSIReset
	}
	parts := make([]string, len(codes))
	for i, c := range codes {
		parts[i] = strconv.Itoa(c)
	}
	return "\x1b[" + strings.Join(parts, ";") + "m"
}

// CellStyle extracts the render style from a vt cell.
func CellStyle(cell vt.Char) Style {
	fg, bg := cell.FG, cell.BG
	if fg == "" {
		fg = "default"
	}
	if bg == "" {
		bg = "default"
	}
	return Style{FG: fg, BG: bg, Bold: cell.Bold, Underscore: cell.Underscore, Reverse: cell.Reverse, Blink: cell.Blink}
}

// AnsiBuffer is a virtual terminal backed by a vt.Screen: feed raw bytes with
// Feed and retrieve ANSI-styled output lines with RenderLines. Port of
// render.buffer.AnsiBuffer.
type AnsiBuffer struct {
	screen *vt.Screen
	stream *vt.Stream
}

// NewAnsiBuffer creates an AnsiBuffer with the given geometry.
func NewAnsiBuffer(cols, rows int) *AnsiBuffer {
	s := vt.NewScreen(cols, rows)
	return &AnsiBuffer{screen: s, stream: vt.NewStream(s)}
}

// Resize resizes the virtual terminal.
func (b *AnsiBuffer) Resize(cols, rows int) {
	b.screen.Resize(rows, cols)
}

// Reset resets the virtual terminal.
func (b *AnsiBuffer) Reset() {
	b.screen.Reset()
}

// Feed decodes raw CP437 bytes through the terminal emulator.
func (b *AnsiBuffer) Feed(data []byte) {
	if len(data) == 0 {
		return
	}
	b.stream.Feed(screen.DecodeCP437(data))
}

// Screen exposes the backing vt.Screen.
func (b *AnsiBuffer) Screen() *vt.Screen {
	return b.screen
}

// RenderLines re-renders the virtual screen as ANSI-styled strings, one per
// row, each ending with a reset so subsequent writes start clean.
func (b *AnsiBuffer) RenderLines(width, height int) []string {
	return RenderScreenLines(b.screen, width, height)
}

// RenderScreenLines walks a vt.Screen's cell buffer and emits SGR escapes
// whenever the style changes between adjacent cells.
func RenderScreenLines(s *vt.Screen, width, height int) []string {
	lines := make([]string, 0, height)
	for y := range height {
		var parts strings.Builder
		haveLast := false
		var last Style
		for x := range width {
			cell := s.At(y, x)
			style := CellStyle(cell)
			char := cell.Data
			if char == "" {
				char = " "
			}
			if !haveLast || style != last {
				parts.WriteString(StyleToSGR(style))
				last = style
				haveLast = true
			}
			parts.WriteString(char)
		}
		parts.WriteString(ANSIReset)
		lines = append(lines, parts.String())
	}
	return lines
}
