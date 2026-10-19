//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"fmt"
	"strconv"
	"strings"
	"unicode/utf8"

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

// FGCodes maps 16-color names to SGR foreground codes. "brown" and
// "brightbrown" are the vt (pyte) names for SGR 33 and 93: without them a
// yellow cell rendered with no colour at all.
var FGCodes = map[string]int{
	"black": 30, "red": 31, "green": 32, "yellow": 33, "brown": 33,
	"blue": 34, "magenta": 35, "cyan": 36, "white": 37,
	"brightblack": 90, "brightred": 91, "brightgreen": 92, "brightyellow": 93, "brightbrown": 93,
	"brightblue": 94, "brightmagenta": 95, "brightcyan": 96, "brightwhite": 97,
}

// BGCodes maps 16-color names to SGR background codes, with the vt names for
// yellow and pyte's spelling of SGR 105 ("bfightmagenta") as well.
var BGCodes = map[string]int{
	"black": 40, "red": 41, "green": 42, "yellow": 43, "brown": 43,
	"blue": 44, "magenta": 45, "cyan": 46, "white": 47,
	"brightblack": 100, "brightred": 101, "brightgreen": 102, "brightyellow": 103, "brightbrown": 103,
	"brightblue": 104, "brightmagenta": 105, "bfightmagenta": 105, "brightcyan": 106, "brightwhite": 107,
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
	FG, BG        string
	Bold          bool
	Underscore    bool
	Reverse       bool
	Blink         bool
	Italics       bool
	Strikethrough bool
}

// DefaultStyle is the style of an absent cell.
var DefaultStyle = Style{FG: "default", BG: "default"}

// StyleToSGR converts pyte cell style attributes to an SGR escape sequence.
// Supports named 16-color, and pyte hex values as truecolor.
//
// The sequence is absolute: it starts from a reset, so it sets exactly this
// style whatever the previous cell's was (relative to it, a cell that dropped
// an attribute kept it, and a background bled along the row). Reverse video
// is SGR 7: swapping the colours instead is no change at all when both are the
// default, and the reversed cell vanished. The attributes go in the order the
// pen's do (1, 3, 4, 5, 7, 9).
func StyleToSGR(s Style) string {
	codes := []int{0}
	if s.Bold {
		codes = append(codes, 1)
	}
	if s.Italics {
		codes = append(codes, 3)
	}
	if s.Underscore {
		codes = append(codes, 4)
	}
	if s.Blink {
		codes = append(codes, 5)
	}
	if s.Reverse {
		codes = append(codes, 7)
	}
	if s.Strikethrough {
		codes = append(codes, 9)
	}
	codes = append(codes, colorSGR(s.FG, true)...)
	codes = append(codes, colorSGR(s.BG, false)...)
	if len(codes) == 1 {
		return ANSIReset
	}
	return sgrSequence(codes)
}

// sgrSequence joins SGR parameters into one escape sequence.
func sgrSequence(codes []int) string {
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
	return Style{
		FG: fg, BG: bg, Bold: cell.Bold, Underscore: cell.Underscore, Reverse: cell.Reverse, Blink: cell.Blink,
		Italics: cell.Italics, Strikethrough: cell.Strikethrough,
	}
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

// placeholder is drawn where a cell holds the empty second half of a wide
// character whose first half is gone, to recreate that cell: see renderRow.
// Any wide character would do; this one is blank should a terminal ever show
// it.
const placeholder = "\u3000"

// RenderScreenLines walks a vt.Screen's cell buffer and emits SGR escapes
// whenever the style changes between adjacent cells. Drawn on fresh rows, the
// rows recreate the cells: see renderRow.
func RenderScreenLines(s *vt.Screen, width, height int) []string {
	lines := make([]string, 0, height)
	for y := range height {
		lines = append(lines, renderRow(s, y, width))
	}
	return lines
}

// isSecondHalf reports whether a cell holds what a wide character's second
// cell does: nothing, or marks.
func isSecondHalf(text string) bool {
	return text == "" || vt.CellWidth(text) == 0
}

// renderRow renders row y so that drawing it on a fresh row recreates every
// cell. Port of render/buffer.py _render_row; every port writes the same
// bytes.
//
// A wide character is drawn once and takes two cells; the cell after it is
// recreated by drawing it, so nothing is written for that cell. Writing a
// space there, as this once did, made every wide character a column wider on
// the terminal it was drawn on, and a row holding one overflowed. Combining
// marks that joined that second cell are written straight after the wide
// character, which joins them to it again; marks joined to the wide character
// itself are written with the column set back onto its second cell, which
// joins them to the wide character instead.
//
// Two cells cannot be recreated by drawing left to right, and get a cursor
// move. A wide character whose second half was overwritten is drawn, and the
// column is then set back onto that half (CHA, not CUB: from the deferred wrap
// at the last column CUB steps one further). A second half whose first half is
// gone (overwritten, or deleted) is recreated the way the stream made it: a
// placeholder wide character drawn in its style, its marks joined to it, and
// the placeholder's first half deleted (DCH). In the last column, where a wide
// character has no second half, the placeholder is drawn one column left
// instead, and the cell there drawn again over its first half. That needs the
// cell before it to be drawable, so one arrangement is left blank: such a cell
// in the last column right after another second half (a wide character's, or
// another such cell). Any marks in it are lost with it.
//
// A cell with marks written into the last column is written with autowrap
// off: the marks join the cell before the cursor, which then rests past the
// last column, and with autowrap on they would first wrap to the next row.
func renderRow(s *vt.Screen, y, cols int) string {
	var parts strings.Builder
	haveLast := false
	var last Style
	put := func(text string, style Style) {
		if !haveLast || style != last {
			parts.WriteString(StyleToSGR(style))
			last = style
			haveLast = true
		}
		parts.WriteString(text)
	}
	putCell := func(text string, style Style, x int) {
		if text == "" {
			return
		}
		if x == cols-1 && (utf8.RuneCountInString(text) > 1 || vt.CellWidth(text) == 0) {
			parts.WriteString("\x1b[?7l")
			put(text, style)
			parts.WriteString("\x1b[?7h")
		} else {
			put(text, style)
		}
	}

	for x := 0; x < cols; {
		cell := s.At(y, x)
		data, style := cell.Data, CellStyle(cell)
		if isSecondHalf(data) {
			if x+1 < cols {
				put(placeholder, style)
				putCell(data, style, x+1)
				fmt.Fprintf(&parts, "\x1b[%dG\x1b[P\x1b[C", x+1)
			} else if x > 0 {
				before := s.At(y, x-1)
				if !isSecondHalf(before.Data) {
					fmt.Fprintf(&parts, "\x1b[%dG", x)
					put(placeholder, style)
					putCell(data, style, x)
					fmt.Fprintf(&parts, "\x1b[%dG", x)
					putCell(before.Data, CellStyle(before), x-1)
				}
			}
			x++
			continue
		}
		if x+1 == cols || vt.CellWidth(data) != 2 {
			putCell(data, style, x)
			x++
			continue
		}
		// A wide character with a cell after it. Its own marks are written
		// with the cursor back on that cell, which joins them to the wide
		// character rather than to it.
		base, size := utf8.DecodeRuneInString(data)
		own := data[size:]
		put(string(base), style)
		after := s.At(y, x+1)
		if isSecondHalf(after.Data) {
			putCell(after.Data, CellStyle(after), x+1)
			if own != "" {
				fmt.Fprintf(&parts, "\x1b[%dG", x+2)
				put(own, style)
				if x+2 < cols {
					fmt.Fprintf(&parts, "\x1b[%dG", x+3)
				}
			}
			x += 2
		} else {
			fmt.Fprintf(&parts, "\x1b[%dG", x+2)
			if own != "" {
				put(own, style)
			}
			x++
		}
	}
	parts.WriteString(ANSIReset)
	return parts.String()
}
