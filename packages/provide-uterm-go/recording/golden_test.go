//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package recording

import (
	"encoding/json"
	"os"
	"reflect"
	"sort"
	"testing"
)

// The stores, held to the Python reference.
//
// testdata/recording_golden.json is what CPython's in-memory and file stores
// return for one lifecycle (regenerate with testdata/gen_recording_golden.py
// from the repository root): get_entries for each (limit, offset, event) — the
// parameters the /api/sessions/{id}/recording/entries route passes through —
// and recording_meta, which the /api/sessions/{id}/recording route serves.

type recordingGolden struct {
	Events  []map[string]any `json:"events"`
	Queries []struct {
		Limit  int              `json:"limit"`
		Offset *int             `json:"offset"`
		Event  *string          `json:"event"`
		Memory []map[string]any `json:"memory"`
		File   []map[string]any `json:"file"`
	} `json:"queries"`
	MemoryAfterEnd     []map[string]any `json:"memory_after_end"`
	FileAfterEnd       []map[string]any `json:"file_after_end"`
	MemoryMeta         map[string]any   `json:"memory_meta"`
	DeterministicMeta  map[string]any   `json:"deterministic_meta"`
	MemoryMetaMissing  map[string]any   `json:"memory_meta_missing"`
	FileMetaExistsKeys []string         `json:"file_meta_exists_keys"`
	FileMetaExists     map[string]any   `json:"file_meta_exists"`
	FileMetaMissing    map[string]any   `json:"file_meta_missing"`
	NullMeta           map[string]any   `json:"null_meta"`
	NullEntries        []map[string]any `json:"null_entries"`
	Limits             []struct {
		Input      int `json:"input"`
		Normalized int `json:"normalized"`
	} `json:"limits"`
}

func loadRecordingGolden(t *testing.T) recordingGolden {
	t.Helper()
	raw, err := os.ReadFile("testdata/recording_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g recordingGolden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

// asJSON round-trips v through JSON, as a reader of the route would see it.
func asJSON(t *testing.T, v any) any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// normaliseEntries strips the lifecycle timestamps the generator strips.
func normaliseEntries(t *testing.T, entries []Event) []map[string]any {
	t.Helper()
	out := []map[string]any{}
	for _, item := range asJSON(t, entries).([]any) {
		m := item.(map[string]any)
		if m["event"] == "log_start" || m["event"] == "log_stop" {
			delete(m, "ts")
		}
		out = append(out, m)
	}
	return out
}

func goldenEvents(g recordingGolden) []Event {
	events := make([]Event, len(g.Events))
	for i, e := range g.Events {
		events[i] = Event(e)
	}
	return events
}

func TestRecordingGoldenQueries(t *testing.T) {
	g := loadRecordingGolden(t)
	stores := map[string]Store{"memory": NewInMemoryStore(), "file": NewLocalFileStore(t.TempDir())}
	for _, s := range stores {
		if err := s.StartSession("s1", map[string]any{"kind": "corpus"}); err != nil {
			t.Fatal(err)
		}
		if err := s.AppendEvents("s1", goldenEvents(g)); err != nil {
			t.Fatal(err)
		}
	}
	for _, q := range g.Queries {
		query := Query{Limit: q.Limit, Offset: q.Offset}
		if q.Event != nil {
			query.Event = *q.Event
		}
		for name, want := range map[string][]map[string]any{"memory": q.Memory, "file": q.File} {
			got, err := stores[name].GetEntries("s1", query)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(normaliseEntries(t, got), want) {
				t.Errorf("%s store, query %+v: got %v, want %v", name, q, normaliseEntries(t, got), want)
			}
		}
	}
	for name, want := range map[string][]map[string]any{"memory": g.MemoryAfterEnd, "file": g.FileAfterEnd} {
		if err := stores[name].EndSession("s1"); err != nil {
			t.Fatal(err)
		}
		got, _ := stores[name].GetEntries("s1", Query{Limit: 200})
		if !reflect.DeepEqual(normaliseEntries(t, got), want) {
			t.Errorf("%s store after end: got %v, want %v", name, normaliseEntries(t, got), want)
		}
	}
	for _, l := range g.Limits {
		if got := normalizeLimit(l.Input); got != l.Normalized {
			t.Errorf("normalizeLimit(%d) = %d, want %d", l.Input, got, l.Normalized)
		}
	}
}

// recording_meta as each store reports it, key for key: the in-memory store
// sizes its events as CPython's json.dumps does, and the file store always
// carries a path, null when there is no file.
func TestRecordingGoldenMeta(t *testing.T) {
	g := loadRecordingGolden(t)

	deterministic := NewInMemoryStore()
	_ = deterministic.AppendEvents("s1", goldenEvents(g))
	meta, _ := deterministic.RecordingMeta("s1")
	if got := asJSON(t, meta); !reflect.DeepEqual(got, any(g.DeterministicMeta)) {
		t.Errorf("deterministic meta = %v, want %v", got, g.DeterministicMeta)
	}
	missing, _ := NewInMemoryStore().RecordingMeta("nosuch")
	if got := asJSON(t, missing); !reflect.DeepEqual(got, any(g.MemoryMetaMissing)) {
		t.Errorf("memory meta for a missing session = %v, want %v", got, g.MemoryMetaMissing)
	}

	file := NewLocalFileStore(t.TempDir())
	_ = file.StartSession("s1", map[string]any{"kind": "corpus"})
	exists, _ := file.RecordingMeta("s1")
	existsJSON := asJSON(t, exists).(map[string]any)
	keys := make([]string, 0, len(existsJSON))
	for k := range existsJSON {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if !reflect.DeepEqual(keys, g.FileMetaExistsKeys) {
		t.Errorf("file meta keys = %v, want %v", keys, g.FileMetaExistsKeys)
	}
	fileMissing, _ := file.RecordingMeta("nosuch")
	if got := asJSON(t, fileMissing); !reflect.DeepEqual(got, any(g.FileMetaMissing)) {
		t.Errorf("file meta for a missing session = %v, want %v", got, g.FileMetaMissing)
	}

	null := NullStore{}
	nullMeta, _ := null.RecordingMeta("s1")
	if got := asJSON(t, nullMeta); !reflect.DeepEqual(got, any(g.NullMeta)) {
		t.Errorf("null meta = %v, want %v", got, g.NullMeta)
	}
	nullEntries, _ := null.GetEntries("s1", Query{})
	if len(nullEntries) != len(g.NullEntries) {
		t.Errorf("null entries = %v, want %v", nullEntries, g.NullEntries)
	}
}
