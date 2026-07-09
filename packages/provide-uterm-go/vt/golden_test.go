//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"testing"
)

// The golden corpus in testdata/golden.jsonl was produced by feeding the
// same inputs to pyte (the Python ground truth) and dumping its state.
// Replaying it here re-verifies parity without needing Python in CI.

type goldenStep struct {
	Feed   *string `json:"feed"`
	Resize []int   `json:"resize"`
}

type goldenCase struct {
	ID       string         `json:"id"`
	Cols     int            `json:"cols"`
	Rows     int            `json:"rows"`
	UseUTF8  *bool          `json:"use_utf8"`
	Steps    []goldenStep   `json:"steps"`
	Expected map[string]any `json:"expected"`
}

// snapshotState captures screen state in the shape the differential
// harness dumps: display, cursor, non-default rendered cells, title, icon
// name, modes, margins and tab stops.
func snapshotState(s *Screen) map[string]any {
	cells := map[string][]any{}
	for y := 0; y < s.Lines(); y++ {
		for x := 0; x < s.Columns(); x++ {
			c := s.At(y, x)
			if c != defaultCharPlain {
				cells[fmt.Sprintf("%d,%d", y, x)] = []any{
					c.Data, c.FG, c.BG, c.Bold, c.Italics, c.Underscore,
					c.Strikethrough, c.Reverse, c.Blink,
				}
			}
		}
	}
	var margins any
	if m, ok := s.Margins(); ok {
		margins = []int{m.Top, m.Bottom}
	}
	cur := s.Cursor()
	return map[string]any{
		"display": s.Display(),
		"cursor": map[string]any{
			"x": cur.X, "y": cur.Y, "hidden": cur.Hidden,
		},
		"cells":     cells,
		"title":     s.Title(),
		"icon_name": s.IconName(),
		"mode":      s.Modes(),
		"margins":   margins,
		"tabstops":  s.TabStops(),
	}
}

// normalizeJSON round-trips v through JSON so numeric and slice types
// compare structurally.
func normalizeJSON(t *testing.T, v any) any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}

func TestGoldenCorpus(t *testing.T) {
	fh, err := os.Open("testdata/golden.jsonl")
	if err != nil {
		t.Fatalf("open golden corpus: %v", err)
	}
	defer func() { _ = fh.Close() }()

	sc := bufio.NewScanner(fh)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	n := 0
	for sc.Scan() {
		var gc goldenCase
		if err := json.Unmarshal(sc.Bytes(), &gc); err != nil {
			t.Fatalf("parse golden case: %v", err)
		}
		n++
		t.Run(gc.ID, func(t *testing.T) {
			screen := NewScreen(gc.Cols, gc.Rows)
			stream := NewStream(screen)
			if gc.UseUTF8 != nil {
				stream.UseUTF8 = *gc.UseUTF8
			}
			for _, step := range gc.Steps {
				if step.Feed != nil {
					stream.Feed(*step.Feed)
				} else {
					screen.Resize(step.Resize[0], step.Resize[1])
				}
			}
			got := normalizeJSON(t, snapshotState(screen))
			want := normalizeJSON(t, gc.Expected)
			if !reflect.DeepEqual(got, want) {
				t.Errorf("state mismatch\n got: %#v\nwant: %#v", got, want)
			}
		})
	}
	if err := sc.Err(); err != nil {
		t.Fatalf("scan golden corpus: %v", err)
	}
	if n < 100 {
		t.Fatalf("suspiciously small golden corpus: %d cases", n)
	}
}
