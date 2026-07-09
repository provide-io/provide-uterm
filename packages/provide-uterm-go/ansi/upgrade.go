//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ansi

import (
	"regexp"
	"slices"
	"strconv"
	"strings"
)

var (
	// sgrRE matches an SGR escape sequence with a possibly-empty
	// semicolon-separated parameter list.
	sgrRE = regexp.MustCompile(`\x1b\[([0-9;]*)m`)
	// tokenRE matches legacy BBS palette tokens {P#}/{T#}.
	tokenRE = regexp.MustCompile(`\{([PT])(\d{1,3})\}`)
)

// normalizeDigits mirrors Python's str(int(p)) for a non-empty ASCII digit
// string: strip leading zeros, keeping a single "0" for all-zero input.
func normalizeDigits(p string) string {
	trimmed := strings.TrimLeft(p, "0")
	if trimmed == "" {
		return "0"
	}
	return trimmed
}

// convertSGR rewrites a single SGR match, replacing 16-color codes via
// mapColor. It leaves the match untouched when the parameter list is empty,
// already contains a "38"/"48" extended-color introducer, or collapses to
// nothing.
func convertSGR(match string, mapColor func(idx int, fg bool) string) string {
	seq := match[2 : len(match)-1]
	if seq == "" {
		return match
	}
	parts := strings.Split(seq, ";")
	if slices.Contains(parts, "38") || slices.Contains(parts, "48") {
		return match
	}
	newParts := make([]string, 0, len(parts))
	for _, p := range parts {
		if p == "" {
			continue
		}
		code, err := strconv.Atoi(p)
		if err != nil {
			// Value overflows int — far outside any color-code range, so it
			// passes through (normalized exactly like Python's str(int(p))).
			newParts = append(newParts, normalizeDigits(p))
			continue
		}
		idx, ok := mapIndex(code)
		if !ok {
			newParts = append(newParts, strconv.Itoa(code))
			continue
		}
		fg := (30 <= code && code <= 37) || (90 <= code && code <= 97)
		newParts = append(newParts, mapColor(idx, fg))
	}
	if len(newParts) == 0 {
		return match
	}
	return "\x1b[" + strings.Join(newParts, ";") + "m"
}

// convertSGR256 rewrites 16-color SGR codes in match to 38;5;N / 48;5;N.
func convertSGR256(match string, palette []int) string {
	return convertSGR(match, func(idx int, fg bool) string {
		color := strconv.Itoa(palette[idx])
		if fg {
			return "38;5;" + color
		}
		return "48;5;" + color
	})
}

// convertTokens256 rewrites {P#}/{T#} tokens to {F#}/{B#} 256-color tokens.
func convertTokens256(text string, palette []int) string {
	return tokenRE.ReplaceAllStringFunc(text, func(m string) string {
		kind := m[1]
		raw, _ := strconv.Atoi(m[2 : len(m)-1])
		color := palette[raw%16]
		prefix := "B"
		if kind == 'P' {
			prefix = "F"
		}
		return "{" + prefix + strconv.Itoa(color) + "}"
	})
}

// convertSGRTC rewrites 16-color SGR codes in match to 38;2;R;G;B / 48;2;R;G;B.
func convertSGRTC(match string, rgbPalette []RGB) string {
	return convertSGR(match, func(idx int, fg bool) string {
		c := rgbPalette[idx]
		rgb := strconv.Itoa(c.R) + ";" + strconv.Itoa(c.G) + ";" + strconv.Itoa(c.B)
		if fg {
			return "38;2;" + rgb
		}
		return "48;2;" + rgb
	})
}

// convertTokensTC rewrites {P#}/{T#} tokens to truecolor SGR escapes.
func convertTokensTC(text string, rgbPalette []RGB) string {
	return tokenRE.ReplaceAllStringFunc(text, func(m string) string {
		kind := m[1]
		raw, _ := strconv.Atoi(m[2 : len(m)-1])
		c := rgbPalette[raw%16]
		rgb := strconv.Itoa(c.R) + ";" + strconv.Itoa(c.G) + ";" + strconv.Itoa(c.B)
		if kind == 'P' {
			return "\x1b[38;2;" + rgb + "m"
		}
		return "\x1b[48;2;" + rgb + "m"
	})
}

// UpgradeTo256 replaces SGR 16-color sequences and {P#}/{T#} tokens with
// 256-color equivalents.
//
// palette is a 16-entry slice mapping BBS color indices to 256-color indices;
// nil selects DefaultPalette.
func UpgradeTo256(text string, palette []int) string {
	pal := palette
	if pal == nil {
		pal = DefaultPalette
	}
	text = convertTokens256(text, pal)
	return sgrRE.ReplaceAllStringFunc(text, func(m string) string {
		return convertSGR256(m, pal)
	})
}

// UpgradeToTruecolor replaces SGR 16-color sequences and {P#}/{T#} tokens
// with 24-bit truecolor equivalents.
//
// palette is a 16-entry slice mapping BBS color indices to 256-color indices
// used to derive RGB values; nil selects DefaultPalette.
func UpgradeToTruecolor(text string, palette []int) string {
	pal := palette
	if pal == nil {
		pal = DefaultPalette
	}
	rgbPalette := paletteToRGB(pal)
	text = convertTokensTC(text, rgbPalette)
	return sgrRE.ReplaceAllStringFunc(text, func(m string) string {
		return convertSGRTC(m, rgbPalette)
	})
}
