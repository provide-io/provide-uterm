//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// testdata/vt_golden.json is the TypeScript port's vt corpus
// (packages/provide-uterm-ts/testdata/gen_vt_golden.py), recorded from pyte;
// scripts/check_protocol_drift.py keeps this copy identical to it. Replaying
// it here holds the Go screen to the same records as the TypeScript and C#
// ones, and the tables scripts/gen_unicode_tables.py generated to the
// reference's: a mismatch means that generator needs rerunning.

type vtCorpusCell struct {
	Y, X                                                     int
	Data, FG, BG                                             string
	Bold, Italics, Underscore, Reverse, Strikethrough, Blink bool
}

type vtCorpusState struct {
	Display []string `json:"display"`
	Cursor  struct {
		X, Y   int
		Hidden bool
	} `json:"cursor"`
	Cells []vtCorpusCell `json:"cells"`
}

type vtCorpusRecord struct {
	Name   string         `json:"name"`
	Cols   int            `json:"cols"`
	Rows   int            `json:"rows"`
	Chunks []string       `json:"chunks"`
	State  *vtCorpusState `json:"state"`
	Pyte   *vtCorpusState `json:"pyte"`
}

type vtCorpus struct {
	Cases           []vtCorpusRecord `json:"cases"`
	DecGraphics     []vtCorpusRecord `json:"dec_graphics_cases"`
	Divergences     []vtCorpusRecord `json:"stall_divergences"`
	ReferenceFixes  []vtCorpusRecord `json:"reference_fix_cases"`
	WcwidthRanges   [][3]int         `json:"wcwidth_ranges"`
	CombiningRanges [][2]int         `json:"combining_ranges"`
	CombiningClass  [][3]int         `json:"combining_classes"`
	Decompositions  [][3]int         `json:"canonical_decompositions"`
	Compositions    [][3]int         `json:"composition_pairs"`
}

func loadVTCorpus(t *testing.T) vtCorpus {
	t.Helper()
	raw, err := os.ReadFile("testdata/vt_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var corpus vtCorpus
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatal(err)
	}
	return corpus
}

// replayVTRecord feeds a record to a fresh screen and fails on any difference
// from the recorded display, cursor or cell.
func replayVTRecord(t *testing.T, record vtCorpusRecord, useUTF8 bool) {
	t.Helper()
	// The divergence records are 20x6 and keep their state under "pyte".
	cols, rows, want := record.Cols, record.Rows, record.State
	if want == nil {
		cols, rows, want = 20, 6, record.Pyte
	}
	s := NewScreen(cols, rows)
	st := NewStream(s)
	st.UseUTF8 = useUTF8
	for _, chunk := range record.Chunks {
		st.Feed(chunk)
	}
	if got := s.Display(); !reflect.DeepEqual(got, want.Display) {
		t.Errorf("%s: display %q, want %q", record.Name, got, want.Display)
	}
	if cur := s.Cursor(); cur.X != want.Cursor.X || cur.Y != want.Cursor.Y || cur.Hidden != want.Cursor.Hidden {
		t.Errorf("%s: cursor (%d,%d,%t), want (%d,%d,%t)", record.Name, cur.X, cur.Y, cur.Hidden,
			want.Cursor.X, want.Cursor.Y, want.Cursor.Hidden)
	}
	cells := map[[2]int]Char{}
	for _, c := range want.Cells {
		cells[[2]int{c.Y, c.X}] = Char{
			Data: c.Data, FG: c.FG, BG: c.BG, Bold: c.Bold, Italics: c.Italics, Underscore: c.Underscore,
			Strikethrough: c.Strikethrough, Reverse: c.Reverse, Blink: c.Blink,
		}
	}
	for y := 0; y < rows; y++ {
		for x := 0; x < cols; x++ {
			wantCell, ok := cells[[2]int{y, x}]
			if !ok {
				wantCell = defaultCharPlain
			}
			if got := s.At(y, x); got != wantCell {
				t.Errorf("%s: cell %d,%d = %+v, want %+v", record.Name, y, x, got, wantCell)
			}
		}
	}
}

func TestVTCorpusMatchesPyte(t *testing.T) {
	corpus := loadVTCorpus(t)
	sections := []struct {
		records []vtCorpusRecord
		useUTF8 bool
	}{
		{corpus.Cases, true},
		{corpus.DecGraphics, false},
		{corpus.Divergences, true},
		{corpus.ReferenceFixes, true},
	}
	total := 0
	for _, section := range sections {
		for _, record := range section.records {
			replayVTRecord(t, record, section.useUTF8)
		}
		total += len(section.records)
	}
	if total < 200 {
		t.Fatalf("only %d records replayed", total)
	}
}

func TestVTCorpusWidthTablesMatchTheReference(t *testing.T) {
	corpus := loadVTCorpus(t)
	widths := map[rune]int{}
	for _, r := range corpus.WcwidthRanges {
		for point := r[0]; point <= r[1]; point++ {
			widths[rune(point)] = r[2]
		}
	}
	combining := map[rune]int{}
	for _, r := range corpus.CombiningClass {
		for point := r[0]; point <= r[1]; point++ {
			combining[rune(point)] = r[2]
		}
	}
	for _, r := range corpus.CombiningRanges {
		for point := r[0]; point <= r[1]; point++ {
			if combining[rune(point)] == 0 {
				t.Fatalf("combining_ranges holds %#x, which has no class", point)
			}
		}
	}
	for point := rune(0); point <= 0x10FFFF; point++ {
		want, ok := widths[point]
		if !ok {
			want = 1
		}
		if got := runeWidth(point); got != want {
			t.Fatalf("runeWidth(%#x) = %d, want %d", point, got, want)
		}
		if got := combiningClass(point); got != combining[point] {
			t.Fatalf("combiningClass(%#x) = %d, want %d", point, got, combining[point])
		}
	}
}

func TestCellWidthIsTheWidthOfTheFirstRune(t *testing.T) {
	for text, want := range map[string]int{"a": 1, "\u4e2d": 2, "\u4e2d\u0301": 2, "\u0301": 0, "\u0301a": 0, "\x01": -1} {
		if got := CellWidth(text); got != want {
			t.Errorf("CellWidth(%q) = %d, want %d", text, got, want)
		}
	}
}

func TestVTCorpusNormalizationTablesMatchTheReference(t *testing.T) {
	corpus := loadVTCorpus(t)
	if len(corpus.Decompositions) != len(canonicalDecomp) || len(corpus.Compositions) != len(compositionPairs) {
		t.Fatalf("tables hold %d decompositions and %d compositions, the reference %d and %d",
			len(canonicalDecomp), len(compositionPairs), len(corpus.Decompositions), len(corpus.Compositions))
	}
	for _, d := range corpus.Decompositions {
		if got := canonicalDecomp[rune(d[0])]; got != [2]rune{rune(d[1]), rune(d[2])} {
			t.Fatalf("canonicalDecomp[%#x] = %v, want %v", d[0], got, d[1:])
		}
	}
	for _, c := range corpus.Compositions {
		if got, ok := compositionPairs[compKey{rune(c[0]), rune(c[1])}]; !ok || got != rune(c[2]) {
			t.Fatalf("compositionPairs[%#x %#x] = %#x, want %#x", c[0], c[1], got, c[2])
		}
	}
}
