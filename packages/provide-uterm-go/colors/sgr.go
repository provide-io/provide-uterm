//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// SGR parameter-list rewriting for color downgrade.
//
// Given an SGR parameter list (the "N;M;..." part between ESC [ and m), scan
// for truecolor runs (38;2;R;G;B foreground or 48;2;R;G;B background) and
// replace them with the configured lower-palette equivalent. Other SGR
// parameters (bold, italic, 256-color, etc.) pass through unchanged.

package colors

import (
	"regexp"
	"strconv"
	"strings"
)

// SGRRegexp matches an SGR escape sequence: `\x1b[` params `m` where params
// is a possibly-empty semicolon-separated parameter list.
var SGRRegexp = regexp.MustCompile(`\x1b\[([0-9;]*)m`)

// Base ANSI 16-color foreground/background escape codes, indexed by palette
// index 0-15 (0-7 are the normal colors, 8-15 the bright variants).
var (
	fg16 = [16]int{30, 34, 32, 36, 31, 35, 33, 37, 90, 94, 92, 96, 91, 95, 93, 97}
	bg16 = [16]int{40, 44, 42, 46, 41, 45, 43, 47, 100, 104, 102, 106, 101, 105, 103, 107}
)

// isDigits reports whether s is a non-empty string of ASCII decimal digits.
func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// parseComponent converts a digit string to an int, saturating values too
// large for int (they are clamped to 255 downstream anyway, matching
// Python's arbitrary-precision int followed by clamp8).
func parseComponent(s string) int {
	trimmed := strings.TrimLeft(s, "0")
	if len(trimmed) > 9 {
		return 1 << 30
	}
	v, _ := strconv.Atoi(s)
	return v
}

// RewriteParams rewrites an SGR parameter list, downgrading any truecolor
// runs.
//
// It walks the ";"-separated parameters looking for the 5-parameter run
// 38;2;R;G;B (foreground truecolor) or 48;2;R;G;B (background truecolor).
// Each such run is replaced with its equivalent under the target mode;
// everything else is preserved in place and order.
//
// params is the SGR parameter list (without the leading "\x1b[" or trailing
// "m") and may be empty. Mode256 maps to the xterm-256 cube; any other mode
// maps to the base 16-color palette.
//
// It returns a full SGR escape sequence ("\x1b[<rewritten>m") ready to be
// re-inserted into the stream.
func RewriteParams(params string, mode ColorMode) string {
	if params == "" {
		return "\x1b[" + params + "m"
	}
	parts := strings.Split(params, ";")
	out := make([]string, 0, len(parts))
	i, n := 0, len(parts)
	for i < n {
		if i+4 < n &&
			(parts[i] == "38" || parts[i] == "48") &&
			parts[i+1] == "2" &&
			isDigits(parts[i+2]) &&
			isDigits(parts[i+3]) &&
			isDigits(parts[i+4]) {
			r := parseComponent(parts[i+2])
			g := parseComponent(parts[i+3])
			b := parseComponent(parts[i+4])
			isFg := parts[i] == "38"
			if mode == Mode256 {
				code := RGBTo256(r, g, b)
				intro := "48"
				if isFg {
					intro = "38"
				}
				out = append(out, intro, "5", strconv.Itoa(code))
			} else {
				idx := RGBTo16Index(r, g, b)
				code := bg16[idx]
				if isFg {
					code = fg16[idx]
				}
				out = append(out, strconv.Itoa(code))
			}
			i += 5
			continue
		}
		out = append(out, parts[i])
		i++
	}
	return "\x1b[" + strings.Join(out, ";") + "m"
}
