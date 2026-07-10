//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"strings"
	"testing"
)

func eqBools(a, b []bool) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// The divergence cases are a 1:1 port of test_fanout_divergence.py so the Go
// SequenceMatcher ratio port is validated against the exact Python outcomes.
func TestComputeDivergence(t *testing.T) {
	cases := []struct {
		name      string
		outputs   []string
		threshold float64
		want      []bool
	}{
		{"all identical", []string{"hello world", "hello world", "hello world"}, 0.9, []bool{false, false, false}},
		{"three identical one different", []string{"hello world", "hello world", "hello world", "completely different text xyz"}, 0.9, []bool{false, false, false, true}},
		{"empty list", []string{}, 0.9, []bool{}},
		{"single output", []string{"only one"}, 0.9, []bool{false}},
		{"all different high threshold", []string{"aaaa", "bbbb", "cccc", "dddd"}, 0.99, []bool{true, true, true, true}},
		{"threshold zero", []string{"totally different", "nothing alike", "xyzzy foobar"}, 0.0, []bool{false, false, false}},
		{"empty string among nonempty", []string{"shell output here", "shell output here", "shell output here", ""}, 0.9, []bool{false, false, false, true}},
		{"majority selection", []string{"aaa", "aab", "aac", "zzz"}, 0.5, []bool{false, false, false, true}},
		{"majority at later index", []string{"zzz", "aaa", "aab", "aac"}, 0.5, []bool{true, false, false, false}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ComputeDivergence(tc.outputs, tc.threshold)
			if !eqBools(got, tc.want) {
				t.Fatalf("ComputeDivergence(%v, %v) = %v, want %v", tc.outputs, tc.threshold, got, tc.want)
			}
		})
	}
}

// TestRatioValues pins a few SequenceMatcher.ratio() values against Python's
// difflib to guard the port (Python: difflib.SequenceMatcher(None,a,b).ratio()).
func TestRatioValues(t *testing.T) {
	cases := []struct {
		a, b string
		want float64
	}{
		{"", "", 1.0},
		{"abcd", "abcd", 1.0},
		{"abcd", "", 0.0},
		{"abcd", "abxd", 0.75}, // 3 matches, T=8 -> 6/8
		{"aaa", "zzz", 0.0},
	}
	for _, tc := range cases {
		got := ratio(tc.a, tc.b)
		if got != tc.want {
			t.Fatalf("ratio(%q,%q)=%v want %v", tc.a, tc.b, got, tc.want)
		}
	}
}

// TestRatioAutojunk exercises the autojunk path (b longer than 200 elements) so
// the popular-element pruning in newSeqMatcher is covered. A very repetitive
// second string triggers the >1% pruning.
func TestRatioAutojunk(t *testing.T) {
	b := strings.Repeat("a", 300) + "unique-tail"
	a := strings.Repeat("a", 300) + "unique-tail"
	// b is >200 elements with a highly-repetitive 'a', so newSeqMatcher's
	// autojunk pruning fires. The ratio must stay a valid similarity in (0,1].
	got := ratio(a, b)
	if got <= 0.0 || got > 1.0 {
		t.Fatalf("autojunk ratio out of range: %v", got)
	}
}
