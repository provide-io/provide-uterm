//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "fmt"

// SelectGraphicRendition sets display attributes on the cursor (SGR).
// Recognized codes match pyte exactly: reset (0), bold/italics/underscore/
// blink/reverse/strikethrough and their resets, 16-color ANSI plus aixterm
// bright colors, and extended 256-color (38;5;N / 48;5;N) and truecolor
// (38;2;R;G;B / 48;2;R;G;B) selections. Unknown codes are ignored.
func (s *Screen) SelectGraphicRendition(attrs ...int) {
	// Fast path for resetting all attributes.
	if len(attrs) == 0 || (len(attrs) == 1 && attrs[0] == 0) {
		s.cursor.Attrs = s.defaultChar()
		return
	}

	work := s.cursor.Attrs
	i := 0
	for i < len(attrs) {
		attr := attrs[i]
		i++
		switch {
		case attr == 0:
			// Reset all attributes.
			work = s.defaultChar()
		case fgANSI[attr] != "":
			work.FG = fgANSI[attr]
		case bgANSI[attr] != "":
			work.BG = bgANSI[attr]
		case applyTextAttr(&work, attr):
			// Text style flag applied.
		case fgAIXTerm[attr] != "":
			work.FG = fgAIXTerm[attr]
		case bgAIXTerm[attr] != "":
			work.BG = bgAIXTerm[attr]
		case attr == 38 || attr == 48:
			i = applyExtendedColor(&work, attr == 38, attrs, i)
		}
	}

	s.cursor.Attrs = work
}

// applyTextAttr applies an SGR text style code to c, reporting whether the
// code was a known style code (pyte.graphics.TEXT).
func applyTextAttr(c *Char, attr int) bool {
	switch attr {
	case 1:
		c.Bold = true
	case 3:
		c.Italics = true
	case 4:
		c.Underscore = true
	case 5:
		c.Blink = true
	case 7:
		c.Reverse = true
	case 9:
		c.Strikethrough = true
	case 22:
		c.Bold = false
	case 23:
		c.Italics = false
	case 24:
		c.Underscore = false
	case 25:
		c.Blink = false
	case 27:
		c.Reverse = false
	case 29:
		c.Strikethrough = false
	default:
		return false
	}
	return true
}

// applyExtendedColor handles the parameters following SGR 38/48 starting
// at attrs[i], returning the next unconsumed index. Out-of-range palette
// indices and truncated parameter lists consume their parameters without
// effect, matching pyte's IndexError swallowing.
func applyExtendedColor(c *Char, isFG bool, attrs []int, i int) int {
	if i >= len(attrs) {
		return i
	}
	n := attrs[i]
	i++
	switch n {
	case 5: // 256-color palette.
		if i >= len(attrs) {
			return i
		}
		m := attrs[i]
		i++
		if m < len(fgBG256) {
			setColor(c, isFG, fgBG256[m])
		}
	case 2: // 24-bit color.
		if i+3 > len(attrs) {
			return len(attrs)
		}
		setColor(c, isFG, fmt.Sprintf("%02x%02x%02x", attrs[i], attrs[i+1], attrs[i+2]))
		i += 3
	}
	return i
}

func setColor(c *Char, isFG bool, color string) {
	if isFG {
		c.FG = color
	} else {
		c.BG = color
	}
}
