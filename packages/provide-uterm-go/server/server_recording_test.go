//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// recServer builds a test server whose Recording dep is store, with one public
// session "s1".
func recServer(t *testing.T, store recording.Store) *testServer {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Recording = store
	})
	ts.reg.add("s1", "admin1", "public")
	return ts
}

func TestRecordingMeta(t *testing.T) {
	store := recording.NewInMemoryStore()
	_ = store.StartSession("s1", map[string]any{"k": "v"})
	_ = store.AppendEvents("s1", []recording.Event{{"ts": 1.0, "event": "output", "data": map[string]any{}}})
	ts := recServer(t, store)

	rec := ts.do("GET", "/api/sessions/s1/recording", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("meta status=%d body=%s", rec.Code, rec.Body.String())
	}
	var meta recording.Meta
	if err := json.Unmarshal(rec.Body.Bytes(), &meta); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !meta.Exists || meta.SizeBytes == 0 || meta.SessionID != "s1" {
		t.Fatalf("unexpected meta: %+v", meta)
	}
}

func TestRecordingMetaUnknownSession404(t *testing.T) {
	ts := recServer(t, recording.NewInMemoryStore())
	rec := ts.do("GET", "/api/sessions/nope/recording", "", adminHeaders())
	if rec.Code != 404 {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestRecordingForbidden(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Recording = recording.NewInMemoryStore()
	})
	ts.reg.add("priv", "someoneelse", "private")
	rec := ts.do("GET", "/api/sessions/priv/recording", "", viewerHeaders())
	if rec.Code != 403 {
		t.Fatalf("want 403, got %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestRecordingEntries(t *testing.T) {
	store := recording.NewInMemoryStore()
	_ = store.StartSession("s1", nil)
	_ = store.AppendEvents("s1", []recording.Event{
		{"ts": 1.0, "event": "output", "data": "a"},
		{"ts": 2.0, "event": "input", "data": "b"},
		{"ts": 3.0, "event": "output", "data": "c"},
	})
	ts := recServer(t, store)

	rec := ts.do("GET", "/api/sessions/s1/recording/entries?event=output&limit=10", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("entries status=%d body=%s", rec.Code, rec.Body.String())
	}
	var entries []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &entries); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(entries) != 2 {
		t.Fatalf("want 2 output events, got %d: %v", len(entries), entries)
	}

	// offset pagination
	rec = ts.do("GET", "/api/sessions/s1/recording/entries?offset=1&limit=1", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("offset status=%d", rec.Code)
	}
	// bad offset → 422
	rec = ts.do("GET", "/api/sessions/s1/recording/entries?offset=-1", "", adminHeaders())
	if rec.Code != 422 {
		t.Fatalf("bad offset: want 422, got %d", rec.Code)
	}
}

func TestRecordingDownload(t *testing.T) {
	dir := t.TempDir()
	store := recording.NewLocalFileStore(dir)
	_ = store.StartSession("s1", map[string]any{"m": 1})
	_ = store.AppendEvents("s1", []recording.Event{{"ts": 1.0, "event": "output", "data": "x"}})
	_ = store.EndSession("s1")

	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Recording.Directory = dir
		deps.Recording = store
	})
	ts.reg.add("s1", "admin1", "public")

	rec := ts.do("GET", "/api/sessions/s1/recording/download", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("download status=%d body=%s", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content-type=%q", ct)
	}
	if cd := rec.Header().Get("Content-Disposition"); cd == "" {
		t.Fatal("missing Content-Disposition")
	}
	if rec.Body.Len() == 0 {
		t.Fatal("empty download body")
	}
}

func TestRecordingDownloadMissing404(t *testing.T) {
	dir := t.TempDir()
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Recording.Directory = dir
		deps.Recording = recording.NewLocalFileStore(dir)
	})
	ts.reg.add("s1", "admin1", "public")
	rec := ts.do("GET", "/api/sessions/s1/recording/download", "", adminHeaders())
	if rec.Code != 404 {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

// TestRecordingDownloadPathConfinement proves a store returning an out-of-tree
// path (outside cfg.Recording.Directory) yields 404, not a file leak.
func TestRecordingDownloadPathConfinement(t *testing.T) {
	outside := t.TempDir()
	leak := filepath.Join(outside, "s1.jsonl")
	if err := os.WriteFile(leak, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Recording.Directory = t.TempDir() // a DIFFERENT trusted dir
		deps.Recording = fixedPathStore{path: leak}
	})
	ts.reg.add("s1", "admin1", "public")
	rec := ts.do("GET", "/api/sessions/s1/recording/download", "", adminHeaders())
	if rec.Code != 404 {
		t.Fatalf("out-of-tree path must 404, got %d", rec.Code)
	}
}

// TestRecordingNilStoreDefaults proves the NullStore default (no Recording dep):
// meta reports absent, entries empty, download 404.
func TestRecordingNilStoreDefaults(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")

	rec := ts.do("GET", "/api/sessions/s1/recording", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("meta status=%d", rec.Code)
	}
	var meta recording.Meta
	_ = json.Unmarshal(rec.Body.Bytes(), &meta)
	if meta.Exists {
		t.Fatal("null store should report exists=false")
	}
	rec = ts.do("GET", "/api/sessions/s1/recording/download", "", adminHeaders())
	if rec.Code != 404 {
		t.Fatalf("null store download: want 404, got %d", rec.Code)
	}
}

// fixedPathStore is a NullStore that returns a fixed GetPath, for the
// path-confinement test.
type fixedPathStore struct {
	recording.NullStore
	path string
}

func (s fixedPathStore) GetPath(string) (string, error) { return s.path, nil }

// errStore is a store whose every read fails, exercising the 500 branches.
type errStore struct{ recording.NullStore }

func (errStore) RecordingMeta(string) (recording.Meta, error) {
	return recording.Meta{}, errBoom
}
func (errStore) GetEntries(string, recording.Query) ([]recording.Event, error) {
	return nil, errBoom
}
func (errStore) GetPath(string) (string, error) { return "", errBoom }

var errBoom = errFixed("boom")

type errFixed string

func (e errFixed) Error() string { return string(e) }

func TestRecordingEntriesBadOffsetNonNumeric(t *testing.T) {
	store := recording.NewInMemoryStore()
	_ = store.StartSession("s1", nil)
	ts := recServer(t, store)
	rec := ts.do("GET", "/api/sessions/s1/recording/entries?offset=abc", "", adminHeaders())
	if rec.Code != 422 {
		t.Fatalf("non-numeric offset: want 422, got %d", rec.Code)
	}
}

// TestRecordingDownloadNonexistentPath covers recordingPathAllowed's
// EvalSymlinks(path) error branch: the store returns a path that does not exist.
func TestRecordingDownloadNonexistentPath(t *testing.T) {
	dir := t.TempDir()
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Recording.Directory = dir
		deps.Recording = fixedPathStore{path: filepath.Join(dir, "ghost.jsonl")}
	})
	ts.reg.add("s1", "admin1", "public")
	rec := ts.do("GET", "/api/sessions/s1/recording/download", "", adminHeaders())
	if rec.Code != 404 {
		t.Fatalf("nonexistent path: want 404, got %d", rec.Code)
	}
}

// TestRecordingPathAllowedBranches exercises recordingPathAllowed directly:
// empty dir, a nonexistent recording file, a nonexistent trusted dir, and the
// happy path.
func TestRecordingPathAllowedBranches(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "a.jsonl")
	if err := os.WriteFile(f, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if recordingPathAllowed("/tmp/whatever.jsonl", "") {
		t.Fatal("empty directory must never be allowed")
	}
	if recordingPathAllowed(filepath.Join(dir, "ghost.jsonl"), dir) {
		t.Fatal("nonexistent file must not be allowed")
	}
	if recordingPathAllowed(f, "/no/such/trusted/dir") {
		t.Fatal("nonexistent trusted dir must not be allowed")
	}
	if !recordingPathAllowed(f, dir) {
		t.Fatal("file inside the trusted dir must be allowed")
	}
}

// TestRecordingGateFailPropagates covers the entries/download gate-fail early
// returns (unknown session → 404 before touching the store).
func TestRecordingGateFailPropagates(t *testing.T) {
	ts := recServer(t, recording.NewInMemoryStore())
	for _, path := range []string{
		"/api/sessions/nope/recording/entries",
		"/api/sessions/nope/recording/download",
	} {
		rec := ts.do("GET", path, "", adminHeaders())
		if rec.Code != 404 {
			t.Fatalf("%s: want 404, got %d", path, rec.Code)
		}
	}
}

func TestRecordingStoreErrors500(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Recording = errStore{}
	})
	ts.reg.add("s1", "admin1", "public")
	for _, path := range []string{
		"/api/sessions/s1/recording",
		"/api/sessions/s1/recording/entries",
		"/api/sessions/s1/recording/download",
	} {
		rec := ts.do("GET", path, "", adminHeaders())
		if rec.Code != 500 {
			t.Fatalf("%s: want 500, got %d", path, rec.Code)
		}
	}
}

// flushingRegistry is the fake registry plus a recording flush, counting the
// flushes per session.
type flushingRegistry struct {
	*fakeRegistry
	mu      sync.Mutex
	flushed []string
	err     error
}

func (r *flushingRegistry) FlushRecording(id string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.flushed = append(r.flushed, id)
	return r.err
}

// A registry that buffers recordings is flushed before the meta and entries
// routes read the store, as the reference's registry flushes the session's
// runtime first (_flush_runtime_recording) — otherwise a reader sees the
// recording as it was up to one flush interval ago.
func TestRecordingRoutesFlushTheSessionFirst(t *testing.T) {
	var reg *flushingRegistry
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Recording = recording.NewInMemoryStore()
		reg = &flushingRegistry{fakeRegistry: deps.Registry.(*fakeRegistry)}
		deps.Registry = reg
	})
	ts.reg.add("s1", "admin1", "public")
	for _, path := range []string{"/api/sessions/s1/recording", "/api/sessions/s1/recording/entries"} {
		if rec := ts.do("GET", path, "", adminHeaders()); rec.Code != 200 {
			t.Fatalf("%s: status=%d", path, rec.Code)
		}
	}
	// A refused read flushes nothing.
	_ = ts.do("GET", "/api/sessions/nope/recording/entries", "", adminHeaders())
	reg.mu.Lock()
	defer reg.mu.Unlock()
	if !reflect.DeepEqual(reg.flushed, []string{"s1", "s1"}) {
		t.Fatalf("flushed = %v, want one flush per read of s1", reg.flushed)
	}
}

// The meta route answers the store's meta plus "enabled", the session's
// recording_enabled, as the reference's registry.recording_meta does.
func TestRecordingMetaCarriesEnabled(t *testing.T) {
	ts := recServer(t, recording.NewInMemoryStore())
	for _, enabled := range []bool{false, true} {
		ts.reg.mu.Lock()
		ts.reg.statuses["s1"].RecordingEnabled = enabled
		ts.reg.mu.Unlock()
		rec := ts.do("GET", "/api/sessions/s1/recording", "", adminHeaders())
		var meta map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &meta); err != nil {
			t.Fatal(err)
		}
		want := map[string]any{"session_id": "s1", "exists": false, "size_bytes": float64(0), "enabled": enabled}
		if !reflect.DeepEqual(meta, want) {
			t.Fatalf("meta = %v, want %v", meta, want)
		}
	}
}

// The entries route validates its query as the reference's does —
// limit 1..500, offset >= 0, event at most 100 characters — answering 422
// rather than clamping a bad value into a good one.
func TestRecordingEntriesValidatesItsQuery(t *testing.T) {
	ts := recServer(t, recording.NewInMemoryStore())
	for query, want := range map[string]int{
		"?limit=1":                           200,
		"?limit=500":                         200,
		"?limit=0":                           422,
		"?limit=501":                         422,
		"?limit=x":                           422,
		"?offset=0":                          200,
		"?offset=-1":                         422,
		"?event=" + strings.Repeat("e", 100): 200,
		"?event=" + strings.Repeat("e", 101): 422,
	} {
		if rec := ts.do("GET", "/api/sessions/s1/recording/entries"+query, "", adminHeaders()); rec.Code != want {
			t.Errorf("%s: status %d, want %d", query, rec.Code, want)
		}
	}
}

// A flush that fails is a 500 rather than a stale read.
func TestRecordingRoutesFailOnAFailedFlush(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Recording = recording.NewInMemoryStore()
		deps.Registry = &flushingRegistry{fakeRegistry: deps.Registry.(*fakeRegistry), err: errors.New("disk full")}
	})
	ts.reg.add("s1", "admin1", "public")
	for _, path := range []string{"/api/sessions/s1/recording", "/api/sessions/s1/recording/entries"} {
		if rec := ts.do("GET", path, "", adminHeaders()); rec.Code != http.StatusInternalServerError {
			t.Fatalf("%s: status=%d, want 500", path, rec.Code)
		}
	}
}
