//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

// NFC normalization, used when a combining character is drawn onto an
// existing cell (pyte calls unicodedata.normalize("NFC", ...)). This is a
// self-contained implementation over the generated canonical decomposition
// and primary composition tables plus algorithmic Hangul handling.

// Hangul syllable composition constants (Unicode chapter 3.12).
const (
	hangulSBase  = 0xAC00
	hangulLBase  = 0x1100
	hangulVBase  = 0x1161
	hangulTBase  = 0x11A7
	hangulLCount = 19
	hangulVCount = 21
	hangulTCount = 28
	hangulNCount = hangulVCount * hangulTCount
	hangulSCount = hangulLCount * hangulNCount
)

// canonicalDecomposeRune appends the full canonical decomposition of r.
func canonicalDecomposeRune(out []rune, r rune) []rune {
	if r >= hangulSBase && r < hangulSBase+hangulSCount {
		sIndex := r - hangulSBase
		out = append(out,
			hangulLBase+sIndex/hangulNCount,
			hangulVBase+(sIndex%hangulNCount)/hangulTCount)
		if t := sIndex % hangulTCount; t != 0 {
			out = append(out, hangulTBase+t)
		}
		return out
	}
	if d, ok := canonicalDecomp[r]; ok {
		out = canonicalDecomposeRune(out, d[0])
		if d[1] >= 0 {
			out = canonicalDecomposeRune(out, d[1])
		}
		return out
	}
	return append(out, r)
}

// canonicalOrder sorts sequences of non-starters by combining class using
// a stable bubble pass (the canonical ordering algorithm).
func canonicalOrder(rs []rune) {
	for i := 1; i < len(rs); i++ {
		cc := combiningClass(rs[i])
		if cc == 0 {
			continue
		}
		j := i
		for j > 0 {
			prev := combiningClass(rs[j-1])
			if prev == 0 || prev <= cc {
				break
			}
			rs[j-1], rs[j] = rs[j], rs[j-1]
			j--
		}
	}
}

// composePair returns the primary composite of a and b, if any.
func composePair(a, b rune) (rune, bool) {
	// Hangul L + V -> LV syllable.
	if a >= hangulLBase && a < hangulLBase+hangulLCount &&
		b >= hangulVBase && b < hangulVBase+hangulVCount {
		return hangulSBase + ((a-hangulLBase)*hangulVCount+(b-hangulVBase))*hangulTCount, true
	}
	// Hangul LV + T -> LVT syllable.
	if a >= hangulSBase && a < hangulSBase+hangulSCount && (a-hangulSBase)%hangulTCount == 0 &&
		b > hangulTBase && b < hangulTBase+hangulTCount {
		return a + (b - hangulTBase), true
	}
	c, ok := compositionPairs[compKey{a, b}]
	return c, ok
}

// composeRunes performs the canonical composition pass in place over a
// decomposed, canonically ordered sequence.
func composeRunes(rs []rune) []rune {
	if len(rs) == 0 {
		return rs
	}
	out := rs[:1]
	starter := -1
	lastCC := combiningClass(rs[0])
	if lastCC == 0 {
		starter = 0
	}
	for _, r := range rs[1:] {
		cc := combiningClass(r)
		// r is blocked from the starter when the preceding character is
		// a non-starter with a combining class >= cc.
		if starter >= 0 && (lastCC < cc || lastCC == 0) {
			if comp, ok := composePair(out[starter], r); ok {
				out[starter] = comp
				continue
			}
		}
		out = append(out, r)
		if cc == 0 {
			starter = len(out) - 1
		}
		lastCC = cc
	}
	return out
}

// nfcNormalize returns the NFC normalization of s, matching
// unicodedata.normalize("NFC", s).
func nfcNormalize(s string) string {
	decomposed := make([]rune, 0, len(s)+2)
	for _, r := range s {
		decomposed = canonicalDecomposeRune(decomposed, r)
	}
	canonicalOrder(decomposed)
	return string(composeRunes(decomposed))
}
