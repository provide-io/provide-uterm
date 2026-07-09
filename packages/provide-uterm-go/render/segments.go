//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"regexp"
	"strconv"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/ansi"
)

// Segment is a run of text sharing one foreground color + bold flag. Color
// is a semantic name ("red", "green", …) or "" for the default, so a
// structured client (web deck UI) can map it onto its own theme rather than
// baking in RGB.
type Segment struct {
	Text  string
	Color string
	Bold  bool
}

// sgrFG maps standard SGR foreground codes to semantic color names. 30-37
// are the base eight; 90-97 are the bright aliases (rendered bold so clients
// without a separate bright palette still distinguish them).
var sgrFG = map[int]string{
	30: "black",
	31: "red",
	32: "green",
	33: "yellow",
	34: "blue",
	35: "magenta",
	36: "cyan",
	37: "white",
}

// SegmentColorNames is the closed set of color names a segment may carry
// ("" = default), in SGR code order.
var SegmentColorNames = []string{"black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"}

var (
	// One SGR sequence: ESC [ <params> m
	segSGRRe = regexp.MustCompile(`^\x1b\[([0-9;]*)m`)
	// Any other CSI / escape (cursor moves, clears) — dropped, no text.
	// [^\n] mirrors Python's non-DOTALL `.`: ESC before a newline is treated
	// as a lone ESC and the newline survives as text.
	segOtherEscRe = regexp.MustCompile(`^(?:\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[^\n])`)
)

// applySGR folds one SGR parameter list into the running (color, bold)
// state.
func applySGR(params, color string, bold bool) (string, bool) {
	var codes []int
	if params == "" {
		codes = []int{0}
	} else {
		for _, p := range strings.Split(params, ";") {
			if p == "" {
				codes = append(codes, 0)
				continue
			}
			c, _ := strconv.Atoi(p)
			codes = append(codes, c)
		}
	}
	for i := 0; i < len(codes); i++ {
		c := codes[i]
		switch {
		case c == 0:
			color, bold = "", false
		case c == 1:
			bold = true
		case c == 22:
			bold = false
		case c == 39:
			color = ""
		case c >= 30 && c <= 37:
			color = sgrFG[c]
		case c >= 90 && c <= 97:
			color, bold = sgrFG[c-60], true
		case c == 38 || c == 48:
			// Extended color: skip its operands (5;n or 2;r;g;b) so they are
			// not mis-read as further SGR codes. Falls back to the default.
			if i+1 < len(codes) && codes[i+1] == 5 {
				i += 2
			} else if i+1 < len(codes) && codes[i+1] == 2 {
				i += 4
			}
		}
	}
	return color, bold
}

// ANSIToSegments parses ANSI-colored text into segments. It recognizes SGR
// color/bold/reset; other escapes (cursor, clear) are dropped. Adjacent runs
// with identical style are merged; empty runs are skipped.
func ANSIToSegments(text string) []Segment {
	var segments []Segment
	color := ""
	bold := false
	var buf strings.Builder

	flush := func() {
		if buf.Len() == 0 {
			return
		}
		chunk := buf.String()
		buf.Reset()
		if n := len(segments); n > 0 && segments[n-1].Color == color && segments[n-1].Bold == bold {
			segments[n-1].Text += chunk
			return
		}
		segments = append(segments, Segment{Text: chunk, Color: color, Bold: bold})
	}

	i := 0
	for i < len(text) {
		if text[i] != 0x1b {
			// Copy the raw byte: multi-byte runes pass through unchanged.
			buf.WriteByte(text[i])
			i++
			continue
		}
		if m := segSGRRe.FindStringSubmatch(text[i:]); m != nil {
			flush()
			color, bold = applySGR(m[1], color, bold)
			i += len(m[0])
			continue
		}
		if m := segOtherEscRe.FindString(text[i:]); m != "" {
			i += len(m) // drop the non-SGR escape, emit no text
			continue
		}
		// Lone ESC with nothing after — skip it.
		i++
	}
	flush()
	return segments
}

// TokensToSegments renders dialect-token text ({+g}…{-x} etc.) into color
// segments — equivalent to ANSIToSegments(ansi.NormalizeColors(text)), so
// the segment colors derive from the same ANSI the terminal renders and
// cannot drift from the token dialect.
func TokensToSegments(text string) []Segment {
	return ANSIToSegments(ansi.NormalizeColors(text))
}
