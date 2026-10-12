//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package recording

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// A session id can be reused (a PAM capture session is named after a pid, and
// pids are reused), so StartSession continues the recording on file only when
// the same instance started it, and otherwise begins afresh.

func recordOne(t *testing.T, s Store, sid string, metadata map[string]any, text string) {
	t.Helper()
	if err := s.StartSession(sid, metadata); err != nil {
		t.Fatal(err)
	}
	if err := s.AppendEvents(sid, []Event{{"ts": 1.0, "event": "read", "data": map[string]any{"text": text}}}); err != nil {
		t.Fatal(err)
	}
	if err := s.EndSession(sid); err != nil {
		t.Fatal(err)
	}
}

func readTexts(t *testing.T, s Store, sid string) []string {
	t.Helper()
	got, err := s.GetEntries(sid, Query{Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, e := range got {
		if e["event"] == "read" {
			out = append(out, e["data"].(map[string]any)["text"].(string))
		}
	}
	return out
}

func bothStores(t *testing.T) map[string]Store {
	return map[string]Store{"file": NewLocalFileStore(t.TempDir()), "memory": NewInMemoryStore()}
}

func TestReusedIDNewInstanceSeesNoneOfTheOldEntries(t *testing.T) {
	for name, s := range bothStores(t) {
		t.Run(name, func(t *testing.T) {
			recordOne(t, s, "pam-u-capture-7", map[string]any{"instance": "first"}, "alice-secret")
			recordOne(t, s, "pam-u-capture-7", map[string]any{"instance": "second"}, "bob-output")
			if got := readTexts(t, s, "pam-u-capture-7"); !slices.Equal(got, []string{"bob-output"}) {
				t.Fatalf("entries = %v, want only [bob-output]", got)
			}
		})
	}
}

func TestStartWithoutInstanceAlwaysBeginsAfresh(t *testing.T) {
	for name, s := range bothStores(t) {
		t.Run(name, func(t *testing.T) {
			recordOne(t, s, "s", map[string]any{}, "one")
			recordOne(t, s, "s", nil, "two")
			if got := readTexts(t, s, "s"); !slices.Equal(got, []string{"two"}) {
				t.Fatalf("entries = %v, want [two]", got)
			}
		})
	}
}

func TestSameInstanceReconnectingKeepsOneRecording(t *testing.T) {
	for name, s := range bothStores(t) {
		t.Run(name, func(t *testing.T) {
			recordOne(t, s, "s", map[string]any{"instance": "i1"}, "first-connection")
			recordOne(t, s, "s", map[string]any{"instance": "i1"}, "second-connection")
			if got := readTexts(t, s, "s"); !slices.Equal(got, []string{"first-connection", "second-connection"}) {
				t.Fatalf("entries = %v, want both connections", got)
			}
		})
	}
}

func TestOldFileIsKeptAsideNotServed(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalFileStore(dir)
	recordOne(t, s, "s", map[string]any{"instance": "first"}, "old")
	recordOne(t, s, "s", map[string]any{"instance": "second"}, "new")

	retired, err := filepath.Glob(filepath.Join(dir, "s.*.jsonl"))
	if err != nil || len(retired) != 1 {
		t.Fatalf("retired files = %v (%v), want exactly one", retired, err)
	}
	old, _ := os.ReadFile(retired[0])
	cur, _ := os.ReadFile(filepath.Join(dir, "s.jsonl"))
	if !strings.Contains(string(old), "old") || strings.Contains(string(cur), "old") {
		t.Fatalf("retired=%q current=%q", old, cur)
	}
	info, _ := os.Stat(retired[0])
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("retired mode = %o, want 600", info.Mode().Perm())
	}
}

func TestUnreadableRecordingIsRetiredNotAppendedTo(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "s.jsonl"), []byte("not json\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	s := NewLocalFileStore(dir)
	recordOne(t, s, "s", map[string]any{"instance": "i1"}, "mine")
	cur, _ := os.ReadFile(filepath.Join(dir, "s.jsonl"))
	if strings.Contains(string(cur), "not json") {
		t.Fatalf("current recording kept the stranger's line: %q", cur)
	}
}

func TestHandleLeftOpenByUnendedInstanceIsNotReused(t *testing.T) {
	s := NewLocalFileStore(t.TempDir())
	if err := s.StartSession("s", map[string]any{"instance": "first"}); err != nil {
		t.Fatal(err)
	}
	if err := s.AppendEvents("s", []Event{{"ts": 1.0, "event": "read", "data": map[string]any{"text": "old"}}}); err != nil {
		t.Fatal(err)
	}
	recordOne(t, s, "s", map[string]any{"instance": "second"}, "new")
	if got := readTexts(t, s, "s"); !slices.Equal(got, []string{"new"}) {
		t.Fatalf("entries = %v, want [new]", got)
	}
}

func TestRetireLeavesNothingToServe(t *testing.T) {
	for name, s := range bothStores(t) {
		t.Run(name, func(t *testing.T) {
			if err := s.StartSession("s", map[string]any{"instance": "a"}); err != nil {
				t.Fatal(err)
			}
			if err := s.AppendEvents("s", []Event{{"ts": 1.0, "event": "read", "data": map[string]any{"text": "old"}}}); err != nil {
				t.Fatal(err)
			}
			if err := Retire(s, "s"); err != nil { // an open handle too: no EndSession first
				t.Fatal(err)
			}
			if got := readTexts(t, s, "s"); len(got) != 0 {
				t.Fatalf("served %v after retire", got)
			}
			if meta, _ := s.RecordingMeta("s"); meta.Exists {
				t.Fatalf("meta %+v after retire", meta)
			}
			recordOne(t, s, "s", map[string]any{"instance": "a"}, "new")
			if got := readTexts(t, s, "s"); !slices.Equal(got, []string{"new"}) {
				t.Fatalf("entries = %v, want [new]", got)
			}
		})
	}
}

type legacyStore struct{ NullStore }

func TestRetireSkipsAStoreWithoutIt(t *testing.T) {
	var s Store = struct{ Store }{legacyStore{}}
	if err := Retire(s, "s"); err != nil {
		t.Fatal(err)
	}
}

// A symlink planted at the recording path is not a recording this store wrote,
// whatever its target says: it is moved aside and its target left untouched.
func TestPlantedSymlinkIsNeverContinued(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(t.TempDir(), "elsewhere.jsonl")
	planted := `{"event":"log_start","data":{"instance":"i1"}}` + "\n"
	if err := os.WriteFile(target, []byte(planted), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(dir, "s.jsonl")); err != nil {
		t.Fatal(err)
	}
	s := NewLocalFileStore(dir)
	recordOne(t, s, "s", map[string]any{"instance": "i1"}, "mine")
	if got := readTexts(t, s, "s"); !slices.Equal(got, []string{"mine"}) {
		t.Fatalf("entries = %v, want [mine]", got)
	}
	if raw, _ := os.ReadFile(target); string(raw) != planted {
		t.Fatalf("the link's target was written: %q", raw)
	}
}

// A first line longer than a log_start can be is not ours either.
func TestOverlongFirstLineIsNeverContinued(t *testing.T) {
	dir := t.TempDir()
	pad := strings.Repeat("x", firstLineLimit)
	old := `{"event":"log_start","data":{"instance":"i1","pad":"` + pad + `"}}` + "\n" +
		`{"ts":1.0,"event":"read","data":{"text":"old"}}` + "\n"
	if err := os.WriteFile(filepath.Join(dir, "s.jsonl"), []byte(old), 0o600); err != nil {
		t.Fatal(err)
	}
	s := NewLocalFileStore(dir)
	recordOne(t, s, "s", map[string]any{"instance": "i1"}, "new")
	if got := readTexts(t, s, "s"); !slices.Equal(got, []string{"new"}) {
		t.Fatalf("entries = %v, want [new]", got)
	}
}
