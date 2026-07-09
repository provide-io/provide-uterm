//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "testing"

func TestNFCNormalize(t *testing.T) {
	// Expected values verified against Python's
	// unicodedata.normalize("NFC", ...) (Unicode 15.1).
	for _, tc := range []struct {
		in, want string
	}{
		{"", ""},
		{"abc", "abc"},
		{"\u00e9", "\u00e9"},               // e + acute -> e-acute
		{"\u1ebf", "\u1ebf"},               // e-circumflex + acute -> single composite
		{"\u00e4\u0301", "\u00e4\u0301"},   // a-diaeresis + non-composing acute
		{"q\u0323\u0307", "q\u0323\u0307"}, // canonical reordering of marks
		{"\u00c5", "\u00c5"},               // singleton: angstrom sign
		{"\u1e0d\u0307", "\u1e0d\u0307"},   // reorder + recompose
		{"\uac00", "\uac00"},               // Hangul L+V
		{"\uac01", "\uac01"},               // Hangul L+V+T
		{"\uac01", "\uac01"},               // Hangul LV+T
		{"\u00c5", "\u00c5"},               // A + ring above
		{"x\u0301", "x\u0301"},             // no composite exists
		{" \u0301", " \u0301"},             // space + acute stays
		{"\u00e1\u0316", "\u00e1\u0316"},   // lower mark reorders + trails
		{"\u00e9\u0300", "\u00e9\u0300"},   // e-acute + grave: no composite
		{"\u1e09", "\u1e09"},               // c-cedilla + acute -> composite
	} {
		if got := nfcNormalize(tc.in); got != tc.want {
			t.Errorf("nfc(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestRuneWidth(t *testing.T) {
	for _, tc := range []struct {
		r    rune
		want int
	}{
		{0, 0},
		{1, -1},
		{0x1b, -1},
		{'a', 1},
		{0x2500, 1},   // box drawing light horizontal
		{0x4f60, 2},   // 你
		{0xff71, 1},   // halfwidth katakana
		{0xff21, 2},   // fullwidth A
		{0x0301, 0},   // combining acute
		{0x200d, 0},   // zero width joiner
		{0x10FFFF, 1}, // unassigned tail
	} {
		if got := runeWidth(tc.r); got != tc.want {
			t.Errorf("runeWidth(%U) = %d, want %d", tc.r, got, tc.want)
		}
	}
}

func TestCombiningClass(t *testing.T) {
	for _, tc := range []struct {
		r    rune
		want int
	}{
		{'a', 0},
		{0x0301, 230}, // acute
		{0x0327, 202}, // cedilla
		{0x0316, 220}, // grave below
		{0x200d, 0},   // ZWJ is not combining
	} {
		if got := combiningClass(tc.r); got != tc.want {
			t.Errorf("combiningClass(%U) = %d, want %d", tc.r, got, tc.want)
		}
	}
}
