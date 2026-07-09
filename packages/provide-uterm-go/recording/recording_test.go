//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package recording

import (
	"os"
	"path/filepath"
	"testing"
)

func intPtr(v int) *int { return &v }

func events(names ...string) []Event {
	out := make([]Event, 0, len(names))
	for i, name := range names {
		out = append(out, Event{"ts": float64(i), "event": name, "data": map[string]any{"i": float64(i)}})
	}
	return out
}

// storeLifecycle exercises the shared Store contract for file and memory
// stores.
func storeLifecycle(t *testing.T, s Store, wantPath bool) {
	t.Helper()
	const sid = "sess-1"

	if err := s.StartSession(sid, map[string]any{"who": "test"}); err != nil {
		t.Fatal(err)
	}
	if err := s.AppendEvents(sid, events("output", "input", "output")); err != nil {
		t.Fatal(err)
	}
	if err := s.EndSession(sid); err != nil {
		t.Fatal(err)
	}

	meta, err := s.RecordingMeta(sid)
	if err != nil {
		t.Fatal(err)
	}
	if !meta.Exists || meta.SizeBytes <= 0 || meta.SessionID != sid {
		t.Fatalf("meta = %+v", meta)
	}

	all, err := s.GetEntries(sid, Query{})
	if err != nil {
		t.Fatal(err)
	}
	// log_start + 3 events + log_stop
	if len(all) != 5 || all[0]["event"] != "log_start" || all[4]["event"] != "log_stop" {
		t.Fatalf("entries = %v", all)
	}
	if all[0]["data"].(map[string]any)["who"] != "test" {
		t.Fatalf("start data = %v", all[0])
	}

	// Event filter.
	outputs, err := s.GetEntries(sid, Query{Event: "output"})
	if err != nil || len(outputs) != 2 {
		t.Fatalf("outputs = %v (%v)", outputs, err)
	}

	// Offset pagination over filtered stream.
	page, err := s.GetEntries(sid, Query{Event: "output", Offset: intPtr(1), Limit: 5})
	if err != nil || len(page) != 1 {
		t.Fatalf("page = %v (%v)", page, err)
	}

	// Tail behavior: last N events.
	tail, err := s.GetEntries(sid, Query{Limit: 2})
	if err != nil || len(tail) != 2 || tail[1]["event"] != "log_stop" {
		t.Fatalf("tail = %v (%v)", tail, err)
	}

	// Limit clamping: limit > 500 behaves like 500; negative clamps to 1.
	if got, _ := s.GetEntries(sid, Query{Limit: 10_000}); len(got) != 5 {
		t.Fatalf("clamped = %v", got)
	}
	if got, _ := s.GetEntries(sid, Query{Limit: -3}); len(got) != 1 {
		t.Fatalf("min-clamped = %v", got)
	}

	// Offset past the end and negative offsets.
	if got, _ := s.GetEntries(sid, Query{Offset: intPtr(99)}); len(got) != 0 {
		t.Fatalf("past-end = %v", got)
	}
	if got, _ := s.GetEntries(sid, Query{Offset: intPtr(-5), Limit: 500}); len(got) != 5 {
		t.Fatalf("neg-offset = %v", got)
	}

	path, err := s.GetPath(sid)
	if err != nil {
		t.Fatal(err)
	}
	if wantPath && path == "" {
		t.Fatal("expected local path")
	}
	if !wantPath && path != "" {
		t.Fatalf("unexpected path %q", path)
	}

	// Unknown session: empty results, no error.
	meta, err = s.RecordingMeta("nope")
	if err != nil || meta.Exists {
		t.Fatalf("meta = %+v (%v)", meta, err)
	}
	if got, _ := s.GetEntries("nope", Query{}); len(got) != 0 {
		t.Fatalf("entries = %v", got)
	}
	if p, _ := s.GetPath("nope"); p != "" {
		t.Fatalf("path = %q", p)
	}
}

func TestLocalFileStoreLifecycle(t *testing.T) {
	storeLifecycle(t, NewLocalFileStore(t.TempDir()), true)
}

func TestInMemoryStoreLifecycle(t *testing.T) {
	storeLifecycle(t, NewInMemoryStore(), false)
}

func TestLocalFileStoreAppendWithoutStartReopens(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalFileStore(dir)
	if err := s.AppendEvents("cold", events("output")); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetEntries("cold", Query{})
	if err != nil || len(got) != 1 {
		t.Fatalf("got %v (%v)", got, err)
	}
	// EndSession without an open handle is a no-op.
	if err := s.EndSession("never-started-other"); err != nil {
		t.Fatal(err)
	}
}

func TestLocalFileStoreSkipsMalformedLines(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalFileStore(dir)
	if err := s.AppendEvents("sess", events("output")); err != nil {
		t.Fatal(err)
	}
	// AppendEvents lazily opened a handle, so EndSession writes log_stop.
	if err := s.EndSession("sess"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "sess.jsonl")
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString("{malformed\n"); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetEntries("sess", Query{})
	if err != nil || len(got) != 2 {
		t.Fatalf("got %v (%v)", got, err)
	}
	// Offset variant also skips malformed lines.
	got, err = s.GetEntries("sess", Query{Offset: intPtr(0)})
	if err != nil || len(got) != 2 {
		t.Fatalf("got %v (%v)", got, err)
	}
	// Offset mode stops reading once the limit is filled.
	got, err = s.GetEntries("sess", Query{Offset: intPtr(0), Limit: 1})
	if err != nil || len(got) != 1 || got[0]["event"] != "output" {
		t.Fatalf("got %v (%v)", got, err)
	}
}

func TestLocalFileStoreStartErrors(t *testing.T) {
	dir := t.TempDir()
	blocker := filepath.Join(dir, "block")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	s := NewLocalFileStore(filepath.Join(blocker, "sub"))
	if err := s.StartSession("x", nil); err == nil {
		t.Fatal("expected error")
	}
	if err := s.AppendEvents("x", events("output")); err == nil {
		t.Fatal("expected error")
	}
}

func TestLocalFileStoreUnserializableEvent(t *testing.T) {
	s := NewLocalFileStore(t.TempDir())
	if err := s.AppendEvents("s", []Event{{"bad": make(chan int)}}); err == nil {
		t.Fatal("expected marshal error")
	}
}

func TestWriteEventsWriteError(t *testing.T) {
	// A read-only handle makes the write itself fail.
	path := filepath.Join(t.TempDir(), "ro.jsonl")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	if err := writeEvents(f, events("output")); err == nil {
		t.Fatal("expected write error")
	}
}

func TestInMemoryStoreMetaSizeAndUnserializable(t *testing.T) {
	s := NewInMemoryStore()
	if err := s.AppendEvents("m", []Event{{"bad": make(chan int)}}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.RecordingMeta("m"); err == nil {
		t.Fatal("expected marshal error in meta size")
	}
	// EndSession on a session never started still records log_stop without
	// flipping a session flag.
	if err := s.EndSession("m"); err != nil {
		t.Fatal(err)
	}
}

func TestNullStore(t *testing.T) {
	var s Store = NullStore{}
	if err := s.StartSession("x", nil); err != nil {
		t.Fatal(err)
	}
	if err := s.AppendEvents("x", events("output")); err != nil {
		t.Fatal(err)
	}
	if err := s.EndSession("x"); err != nil {
		t.Fatal(err)
	}
	meta, err := s.RecordingMeta("x")
	if err != nil || meta.Exists || meta.SizeBytes != 0 {
		t.Fatalf("meta = %+v (%v)", meta, err)
	}
	got, err := s.GetEntries("x", Query{})
	if err != nil || len(got) != 0 {
		t.Fatalf("got %v (%v)", got, err)
	}
	if p, _ := s.GetPath("x"); p != "" {
		t.Fatalf("path = %q", p)
	}
}
