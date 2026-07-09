//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ansi

import (
	"fmt"
	"regexp"
	"strconv"
)

// previewColorMap maps color characters to base SGR foreground codes.
var previewColorMap = map[string]int{
	"k": 30,
	"r": 31,
	"g": 32,
	"y": 33,
	"b": 34,
	"m": 35,
	"c": 36,
	"w": 37,
}

// tildeMap maps tilde codes to a (polarity, color character) pair.
var tildeMap = map[string][2]string{
	"1": {"+", "g"},
	"2": {"+", "w"},
	"3": {"+", "c"},
	"4": {"+", "r"},
	"5": {"+", "m"},
	"6": {"+", "y"},
	"7": {"-", "w"},
	"0": {"-", "x"},
	"r": {"+", "r"},
	"R": {"+", "r"},
	"g": {"+", "g"},
	"G": {"+", "g"},
	"y": {"+", "y"},
	"Y": {"+", "y"},
	"b": {"+", "b"},
	"B": {"+", "b"},
	"m": {"+", "m"},
	"M": {"+", "m"},
	"c": {"+", "c"},
	"C": {"+", "c"},
	"w": {"+", "w"},
	"W": {"+", "w"},
	"d": {"-", "w"},
	"D": {"-", "w"},
	"E": {"+", "r"},
}

// braceTokenMap maps TWGS brace tokens to ANSI escapes.
var braceTokenMap = map[string]string{
	"{+c}":  "\x1b[1;36m",
	"{-c}":  "\x1b[0;36m",
	"{+r}":  "\x1b[1;31m",
	"{-r}":  "\x1b[0;31m",
	"{+g}":  "\x1b[1;32m",
	"{-g}":  "\x1b[0;32m",
	"{+y}":  "\x1b[1;33m",
	"{-y}":  "\x1b[0;33m",
	"{+b}":  "\x1b[1;34m",
	"{-b}":  "\x1b[0;34m",
	"{+m}":  "\x1b[1;35m",
	"{-m}":  "\x1b[0;35m",
	"{+w}":  "\x1b[1;37m",
	"{+Bw}": "\x1b[1;37m",
	"{-w}":  "\x1b[0;37m",
	"{+k}":  "\x1b[1;30m",
	"{-k}":  "\x1b[0;30m",
	"{-x}":  "\x1b[0m",
	"{NK}":  "\x1b[0m",
	"{T}":   "\x1b[1m",
	"{t}":   "\x1b[0m",
}

// emitColor renders a (polarity, color character) pair as an ANSI escape.
// Unknown color characters yield the empty string.
func emitColor(polarity, colorChar string) string {
	if colorChar == "x" {
		return "\x1b[0m"
	}
	code, ok := previewColorMap[colorChar]
	if !ok {
		return ""
	}
	if polarity == "+" {
		return fmt.Sprintf("\x1b[0;1;%dm", code)
	}
	return fmt.Sprintf("\x1b[0;%dm", code)
}

// extTokenRE matches {F###}/{B###}/{P#}/{T#} extended color tokens.
var extTokenRE = regexp.MustCompile(`\{([FBPT])(\d{1,3})\}`)

// extPCode returns the SGR foreground code for a {P#} palette index 0-15.
func extPCode(i int) int {
	if i >= 8 {
		return 90 + (i - 8)
	}
	return 30 + i
}

// extTCode returns the SGR background code for a {T#} palette index 0-15.
func extTCode(i int) int {
	if i >= 8 {
		return 100 + (i - 8)
	}
	return 40 + i
}

// handleExtendedTokens converts {F###}/{B###}/{P#}/{T#} extended color
// tokens to ANSI escapes.
func handleExtendedTokens(text string) string {
	return extTokenRE.ReplaceAllStringFunc(text, func(m string) string {
		kind := m[1]
		val, _ := strconv.Atoi(m[2 : len(m)-1])
		switch kind {
		case 'F':
			return fmt.Sprintf("\x1b[38;5;%dm", val)
		case 'B':
			return fmt.Sprintf("\x1b[48;5;%dm", val)
		case 'P':
			return fmt.Sprintf("\x1b[%dm", extPCode(val%16))
		default: // 'T' — truecolor reserved for future use
			return fmt.Sprintf("\x1b[%dm", extTCode(val%16))
		}
	})
}

// tildeRE matches a tilde followed by any single character.
var tildeRE = regexp.MustCompile(`~(.)`)

// tildeLookup maps tilde codes to ANSI escapes, pre-built from tildeMap.
var tildeLookup = buildTildeLookup()

func buildTildeLookup() map[string]string {
	m := make(map[string]string, len(tildeMap))
	for code, pc := range tildeMap {
		if seq := emitColor(pc[0], pc[1]); seq != "" {
			m[code] = seq
		}
	}
	return m
}

// handleTildeCodes converts ~N tilde codes to ANSI escapes. Unknown codes
// pass through unchanged.
func handleTildeCodes(text string) string {
	return tildeRE.ReplaceAllStringFunc(text, func(m string) string {
		if seq, ok := tildeLookup[m[1:]]; ok {
			return seq
		}
		return m
	})
}

var (
	// brace3RE matches {+c}/{-x} style tokens plus {NK}/{T}/{t}.
	brace3RE = regexp.MustCompile(`\{[+\-][a-zA-Z]\}|\{NK\}|\{T\}|\{t\}`)
	// brace4RE matches the TWGS-specific {+Bw}/{-Bw} header tokens.
	brace4RE = regexp.MustCompile(`\{[+\-]Bw\}`)
)

// replaceBraceToken maps a matched brace token through braceTokenMap,
// leaving unknown tokens untouched.
func replaceBraceToken(tok string) string {
	if seq, ok := braceTokenMap[tok]; ok {
		return seq
	}
	return tok
}

// handleBraceTokens converts {+c}/{-x} brace tokens to ANSI escapes.
//
// It includes the TWGS-specific {+Bw} header token in addition to the
// single-character color tags. Longer tokens are handled first to avoid
// conflicts.
func handleBraceTokens(text string) string {
	text = brace4RE.ReplaceAllStringFunc(text, replaceBraceToken)
	return brace3RE.ReplaceAllStringFunc(text, replaceBraceToken)
}

// pipeRE matches |NN pipe codes (exactly two digits).
var pipeRE = regexp.MustCompile(`\|(\d{2})`)

// DOS color order → ANSI SGR codes.
var (
	dosToANSIFg = [8]int{30, 34, 32, 36, 31, 35, 33, 37} // |00-|07 (dim)
	dosToANSIBg = [8]int{40, 44, 42, 46, 41, 45, 43, 47} // |16-|23
)

// pipeLookup maps two-digit pipe codes "00"-"23" to ANSI escapes.
var pipeLookup = buildPipeLookup()

func buildPipeLookup() map[string]string {
	m := make(map[string]string, 24)
	for i := range 24 {
		key := fmt.Sprintf("%02d", i)
		switch {
		case i <= 7:
			m[key] = fmt.Sprintf("\x1b[%dm", dosToANSIFg[i])
		case i <= 15:
			m[key] = fmt.Sprintf("\x1b[%dm", dosToANSIFg[i-8]+60)
		default:
			m[key] = fmt.Sprintf("\x1b[%dm", dosToANSIBg[i-16])
		}
	}
	return m
}

// handlePipeCodes converts |00-|23 pipe codes to ANSI escapes. Codes outside
// the known range pass through unchanged.
func handlePipeCodes(text string) string {
	return pipeRE.ReplaceAllStringFunc(text, func(m string) string {
		if seq, ok := pipeLookup[m[1:]]; ok {
			return seq
		}
		return m
	})
}

// mustRegister registers a built-in dialect, panicking on duplicate names
// (impossible at init time).
func mustRegister(name string, handler func(string) string) {
	if err := RegisterColorDialect(name, handler); err != nil {
		panic(err)
	}
}

// Register built-in dialects in the same order as the Python implementation.
func init() {
	mustRegister("brace_tokens", handleBraceTokens)
	mustRegister("extended_tokens", handleExtendedTokens)
	mustRegister("tilde_codes", handleTildeCodes)
	mustRegister("pipe_codes", handlePipeCodes)
}
