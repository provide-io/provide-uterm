//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"math/rand/v2"
	"regexp"
	"strings"
	"testing"
)

// Mirrors tests/terminal/test_split_incomplete_escape.py: SplitIncompleteEscape
// carries exactly what StripANSI would strip once complete. For every
// sequence StripANSI strips (no longer than MaxEscapeCarry), cut at every
// position, splitting the first part and carrying its tail into the second
// strips to exactly what stripping the whole text does -- and a cut strictly
// inside the sequence carries all of its head.

const (
	escPrefix = "ab"
	escSuffix = "\n!"
)

var wholeEscapeRE = regexp.MustCompile(`\A(?:` + ansiEscapeRE.String() + `)\z`)

func feSequences() []string {
	var out []string
	for c := byte(0x40); c < 0x60; c++ {
		out = append(out, "\x1b"+string(rune(c)))
	}
	return out
}

func csiSequences() []string {
	var out []string
	for _, p := range []string{"", "1", "38;2;255;0;0", "?25", "<=>", "0;1:2"} {
		for _, i := range []string{"", " ", "!", "/ "} {
			for f := byte(0x40); f < 0x7f; f++ {
				out = append(out, "\x1b["+p+i+string(rune(f)))
			}
		}
	}
	return out
}

func assertCarriedCorrectly(t *testing.T, seq string) {
	t.Helper()
	if !wholeEscapeRE.MatchString(seq) {
		t.Fatalf("%q is not a sequence StripANSI strips", seq)
	}
	expected := StripANSI(escPrefix + seq + escSuffix)
	if expected != escPrefix+escSuffix {
		t.Fatalf("StripANSI(%q) = %q", seq, expected)
	}
	for cut := 0; cut <= len(seq); cut++ {
		head := escPrefix + seq[:cut]
		complete, carry := SplitIncompleteEscape(head, MaxEscapeCarry)
		if complete+carry != head {
			t.Fatalf("%q cut %d: (%q, %q) does not reassemble", seq, cut, complete, carry)
		}
		if cut > 0 && cut < len(seq) && carry != seq[:cut] {
			t.Fatalf("%q cut %d: carry %q, want the whole head", seq, cut, carry)
		}
		if cut == 0 && carry != "" {
			t.Fatalf("%q cut 0: carry %q", seq, carry)
		}
		if got := StripANSI(complete) + StripANSI(carry+seq[cut:]+escSuffix); got != expected {
			t.Fatalf("%q cut %d: stripped %q, want %q", seq, cut, got, expected)
		}
	}
}

func TestEveryEnumeratedSequenceIsCarriedCorrectlyAtEveryCut(t *testing.T) {
	seqs := append(feSequences(), csiSequences()...)
	if len(seqs) != 32+6*4*63 {
		t.Fatalf("enumerated %d sequences", len(seqs))
	}
	for _, seq := range seqs {
		assertCarriedCorrectly(t, seq)
	}
}

// randomSequence draws a sequence from StripANSI's grammar, built from the
// same byte classes: Fe, or CSI with random parameter and intermediate runs.
func randomSequence(r *rand.Rand) string {
	if r.IntN(4) == 0 {
		return "\x1b" + string(rune(0x40+r.IntN(0x20)))
	}
	var b strings.Builder
	b.WriteString("\x1b[")
	for range r.IntN(40) {
		b.WriteByte(byte(0x30 + r.IntN(0x10)))
	}
	for range r.IntN(8) {
		b.WriteByte(byte(0x20 + r.IntN(0x10)))
	}
	b.WriteByte(byte(0x40 + r.IntN(0x3f)))
	return b.String()
}

func TestAnySequenceStripANSIStripsIsCarriedCorrectlyAtEveryCut(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	for range 2000 {
		if seq := randomSequence(r); len(seq) <= MaxEscapeCarry {
			assertCarriedCorrectly(t, seq)
		}
	}
}

func TestSplitIncompleteEscapeExamples(t *testing.T) {
	atBound := "\x1b[" + strings.Repeat("1", MaxEscapeCarry-2)
	pastBound := "\x1b[" + strings.Repeat("1", MaxEscapeCarry-1)
	for _, c := range []struct{ in, complete, carry string }{
		{"", "", ""},
		{"plain", "plain", ""},
		{"a\x1b", "a", "\x1b"},
		{"a\x1b[", "a", "\x1b["},
		{"a\x1b[38;2;255", "a", "\x1b[38;2;255"},
		{"a\x1b[1 ", "a", "\x1b[1 "},           // intermediate byte, still no final
		{"a\x1b[1mb", "a\x1b[1mb", ""},         // complete
		{"a\x1b[1m", "a\x1b[1m", ""},           // complete at the very end
		{"a\x1bc", "a\x1bc", ""},               // complete two-byte sequence
		{"\x1b[1m\x1b[3", "\x1b[1m", "\x1b[3"}, // only the last ESC counts
		{"a\x1b[1é", "a\x1b[1é", ""},           // a byte no sequence carries
		{atBound, "", atBound},
		{pastBound, pastBound, ""},
	} {
		complete, carry := SplitIncompleteEscape(c.in, MaxEscapeCarry)
		if complete != c.complete || carry != c.carry {
			t.Errorf("SplitIncompleteEscape(%q) = (%q, %q), want (%q, %q)", c.in, complete, carry, c.complete, c.carry)
		}
	}
}

func TestMaxCarryIsConfigurableAndDefaultsTo64(t *testing.T) {
	if MaxEscapeCarry != 64 {
		t.Fatalf("MaxEscapeCarry = %d", MaxEscapeCarry)
	}
	if c, k := SplitIncompleteEscape("x\x1b[12", 4); c != "x" || k != "\x1b[12" {
		t.Fatalf("at the bound: (%q, %q)", c, k)
	}
	if c, k := SplitIncompleteEscape("x\x1b[123", 4); c != "x\x1b[123" || k != "" {
		t.Fatalf("past the bound: (%q, %q)", c, k)
	}
}

// The two patterns are built from the same pieces: the reference's literal
// strings, assembled.
func TestTheEscapeGrammarMatchesTheReference(t *testing.T) {
	if got := ansiEscapeRE.String(); got != "\x1b(?:\\[[0-?]*[ -/]*[@-~]|[@-_])" {
		t.Fatalf("ansiEscapeRE = %q", got)
	}
	if got := incompleteEscapeRE.String(); got != "\\A\x1b(?:\\[[0-?]*[ -/]*)?\\z" {
		t.Fatalf("incompleteEscapeRE = %q", got)
	}
}
