//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "sort"

// lookupRange finds r in a sorted []widthRange table and returns its value.
func lookupRange(table []widthRange, r rune) (int, bool) {
	i := sort.Search(len(table), func(i int) bool { return table[i].hi >= r })
	if i < len(table) && table[i].lo <= r {
		return table[i].val, true
	}
	return 0, false
}

// runeWidth returns the printable cell width of r, matching the Python
// wcwidth library pyte uses: -1 for unprintable control characters, 0 for
// zero-width characters, 2 for wide characters and 1 otherwise.
func runeWidth(r rune) int {
	if v, ok := lookupRange(wcwidthRanges, r); ok {
		return v
	}
	return 1
}

// combiningClass returns the canonical combining class of r, matching
// unicodedata.combining (0 for starters and unassigned code points).
func combiningClass(r rune) int {
	if v, ok := lookupRange(combiningRanges, r); ok {
		return v
	}
	return 0
}
