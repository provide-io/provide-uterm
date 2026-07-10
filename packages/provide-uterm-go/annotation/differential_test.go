//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"encoding/json"
	"os"
	"testing"
)

// Differential test: replay the Python-built corpus through the Go port and
// assert byte-identical decisions. The golden is produced by
// scratchpad/dump_annotation_differential.py and committed under testdata/.

type diffChunk struct {
	EventType string `json:"event_type"`
	Text      string `json:"text"`
	Seq       int    `json:"seq"`
}

type diffGolden struct {
	DetectCases []struct {
		EventType   string           `json:"event_type"`
		Text        string           `json:"text"`
		Seq         int              `json:"seq"`
		Annotations []map[string]any `json:"annotations"`
		MaxEnd      int              `json:"max_end"`
	} `json:"detect_cases"`
	StreamCases []struct {
		MaxCarry int                `json:"max_carry"`
		Chunks   []diffChunk        `json:"chunks"`
		Outputs  [][]map[string]any `json:"outputs"`
	} `json:"stream_cases"`
}

func loadAnnotationGolden(t *testing.T) diffGolden {
	t.Helper()
	data, err := os.ReadFile("testdata/differential_golden.json")
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var g diffGolden
	if err := json.Unmarshal(data, &g); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	return g
}

func canonicalJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(b)
}

// annsToDicts converts a (possibly nil) slice of annotations into a non-nil
// slice of dicts so the JSON encoding matches Python's list-of-dicts ([] not
// null) for the empty case.
func annsToDicts(anns []Annotation) []map[string]any {
	out := make([]map[string]any, 0, len(anns))
	for _, a := range anns {
		out = append(out, a.ToDict())
	}
	return out
}

func TestDifferentialDetect(t *testing.T) {
	g := loadAnnotationGolden(t)
	if len(g.DetectCases) == 0 {
		t.Fatal("no detect cases in golden")
	}
	det := NewPatternDetector(nil)
	for _, c := range g.DetectCases {
		anns, maxEnd := det.Scan(c.EventType, c.Text, c.Seq)
		if maxEnd != c.MaxEnd {
			t.Errorf("max_end mismatch for %q: got %d want %d", c.Text, maxEnd, c.MaxEnd)
		}
		got := canonicalJSON(t, annsToDicts(anns))
		want := canonicalJSON(t, ensureNonNil(c.Annotations))
		if got != want {
			t.Errorf("annotation mismatch for %q\n got: %s\nwant: %s", c.Text, got, want)
		}
	}
}

func TestDifferentialStreaming(t *testing.T) {
	g := loadAnnotationGolden(t)
	if len(g.StreamCases) == 0 {
		t.Fatal("no stream cases in golden")
	}
	for ci, c := range g.StreamCases {
		sd := NewStreamingDetector(NewPatternDetector(nil), c.MaxCarry)
		for i, chunk := range c.Chunks {
			out := sd.Detect(chunk.EventType, chunk.Text, chunk.Seq)
			got := canonicalJSON(t, annsToDicts(out))
			want := canonicalJSON(t, ensureNonNil(c.Outputs[i]))
			if got != want {
				t.Errorf("stream case %d chunk %d mismatch\n got: %s\nwant: %s", ci, i, got, want)
			}
		}
	}
}

func ensureNonNil(v []map[string]any) []map[string]any {
	if v == nil {
		return []map[string]any{}
	}
	return v
}
