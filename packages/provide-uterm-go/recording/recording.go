//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package recording provides recording-store backends for terminal session
// capture. Port of provide.uterm.recording: the Store interface plus three
// reference implementations — LocalFileStore (JSONL files), InMemoryStore
// (ephemeral), and NullStore (no-op).
package recording

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/fileio"
)

// Event is one JSON-serializable recording entry. Each event has at minimum
// "ts", "event", and "data" keys.
type Event = map[string]any

// Meta describes a recording. Exists/SizeBytes are always present; Path is
// non-empty only for stores with a local file.
type Meta struct {
	SessionID string `json:"session_id"`
	Exists    bool   `json:"exists"`
	SizeBytes int64  `json:"size_bytes"`
	Path      string `json:"path,omitempty"`
}

// Query selects entries from a recording. Limit is clamped to 1..500 (zero
// selects the default of 200). When Offset is nil the tail (last Limit
// matching events) is returned; otherwise Offset matching events are skipped
// from the start. Event, when non-empty, filters by event type.
type Query struct {
	Limit  int
	Offset *int
	Event  string
}

// Store is the interface for persisting and retrieving session recordings.
// The lifecycle is StartSession → AppendEvents (repeatedly) → EndSession;
// query methods may be called at any time, including mid-session.
type Store interface {
	StartSession(sessionID string, metadata map[string]any) error
	AppendEvents(sessionID string, events []Event) error
	EndSession(sessionID string) error
	RecordingMeta(sessionID string) (Meta, error)
	GetEntries(sessionID string, q Query) ([]Event, error)
	// GetPath returns a local file path for the recording, or "" for stores
	// with no local file.
	GetPath(sessionID string) (string, error)
}

func normalizeLimit(limit int) int {
	if limit == 0 {
		limit = 200
	}
	return max(1, min(limit, 500))
}

func lifecycleEvent(name, sessionID string, data map[string]any) Event {
	if data == nil {
		data = map[string]any{}
	}
	return Event{
		"ts":         float64(time.Now().UnixNano()) / 1e9,
		"event":      name,
		"data":       data,
		"session_id": sessionID,
	}
}

// ---------------------------------------------------------------------------
// LocalFileStore
// ---------------------------------------------------------------------------

// LocalFileStore is a file-backed Store using one JSONL file per session,
// opened via fileio.SecureOpenAppend (owner-only perms, no symlink follow).
type LocalFileStore struct {
	directory string
	mu        sync.Mutex
	files     map[string]*os.File
}

// NewLocalFileStore creates a store rooted at directory.
func NewLocalFileStore(directory string) *LocalFileStore {
	return &LocalFileStore{directory: directory, files: map[string]*os.File{}}
}

func (s *LocalFileStore) path(sessionID string) string {
	return filepath.Join(s.directory, sessionID+".jsonl")
}

func writeEvents(f *os.File, events []Event) error {
	for _, event := range events {
		line, err := json.Marshal(event)
		if err != nil {
			return err
		}
		if _, err := f.Write(append(line, '\n')); err != nil {
			return err
		}
	}
	return f.Sync()
}

// StartSession opens the session file and writes the log_start event.
func (s *LocalFileStore) StartSession(sessionID string, metadata map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := fileio.SecureOpenAppend(s.path(sessionID))
	if err != nil {
		return err
	}
	s.files[sessionID] = f
	return writeEvents(f, []Event{lifecycleEvent("log_start", sessionID, metadata)})
}

// AppendEvents appends a batch of events, reopening the file if needed.
func (s *LocalFileStore) AppendEvents(sessionID string, events []Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, ok := s.files[sessionID]
	if !ok {
		var err error
		f, err = fileio.SecureOpenAppend(s.path(sessionID))
		if err != nil {
			return err
		}
		s.files[sessionID] = f
	}
	return writeEvents(f, events)
}

// EndSession writes the log_stop event and closes the file.
func (s *LocalFileStore) EndSession(sessionID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, ok := s.files[sessionID]
	if !ok {
		return nil
	}
	delete(s.files, sessionID)
	err := writeEvents(f, []Event{lifecycleEvent("log_stop", sessionID, nil)})
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	return err
}

// RecordingMeta reports existence, path, and size of the session file.
func (s *LocalFileStore) RecordingMeta(sessionID string) (Meta, error) {
	path := s.path(sessionID)
	info, err := os.Stat(path)
	if err != nil {
		return Meta{SessionID: sessionID}, nil //nolint:nilerr // absent file == exists:false, like Python
	}
	return Meta{SessionID: sessionID, Exists: true, Path: path, SizeBytes: info.Size()}, nil
}

// GetEntries reads paginated events from the JSONL file. Malformed lines are
// skipped, matching the Python JSONDecodeError-continue behavior.
func (s *LocalFileStore) GetEntries(sessionID string, q Query) ([]Event, error) {
	f, err := os.Open(s.path(sessionID))
	if err != nil {
		return []Event{}, nil //nolint:nilerr // absent file == empty result, like Python
	}
	defer func() { _ = f.Close() }()

	limit := normalizeLimit(q.Limit)
	entries := []Event{}
	skipped := 0
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		var item Event
		if err := json.Unmarshal(scanner.Bytes(), &item); err != nil {
			continue
		}
		if q.Event != "" && item["event"] != q.Event {
			continue
		}
		if q.Offset != nil {
			if skipped < *q.Offset {
				skipped++
				continue
			}
			entries = append(entries, item)
			if len(entries) >= limit {
				break
			}
			continue
		}
		// Tail behavior: keep only the last limit events.
		entries = append(entries, item)
		if len(entries) > limit {
			entries = entries[1:]
		}
	}
	return entries, scanner.Err()
}

// GetPath returns the session file path if it exists.
func (s *LocalFileStore) GetPath(sessionID string) (string, error) {
	path := s.path(sessionID)
	if _, err := os.Stat(path); err != nil {
		return "", nil //nolint:nilerr // absent file == no path, like Python
	}
	return path, nil
}

// ---------------------------------------------------------------------------
// InMemoryStore
// ---------------------------------------------------------------------------

// InMemoryStore keeps all events in memory. Useful for tests and as a
// reference implementation for custom remote stores; data is lost when the
// process exits.
type InMemoryStore struct {
	mu       sync.Mutex
	sessions map[string]bool // session_id → active
	events   map[string][]Event
}

// NewInMemoryStore creates an empty in-memory store.
func NewInMemoryStore() *InMemoryStore {
	return &InMemoryStore{sessions: map[string]bool{}, events: map[string][]Event{}}
}

// StartSession records the log_start event and marks the session active.
func (s *InMemoryStore) StartSession(sessionID string, metadata map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sessions[sessionID] = true
	s.events[sessionID] = append(s.events[sessionID], lifecycleEvent("log_start", sessionID, metadata))
	return nil
}

// AppendEvents appends a batch of events.
func (s *InMemoryStore) AppendEvents(sessionID string, events []Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events[sessionID] = append(s.events[sessionID], events...)
	return nil
}

// EndSession records the log_stop event and marks the session inactive.
func (s *InMemoryStore) EndSession(sessionID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events[sessionID] = append(s.events[sessionID], lifecycleEvent("log_stop", sessionID, nil))
	if _, ok := s.sessions[sessionID]; ok {
		s.sessions[sessionID] = false
	}
	return nil
}

// RecordingMeta reports existence and the serialized size of stored events.
func (s *InMemoryStore) RecordingMeta(sessionID string) (Meta, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	events := s.events[sessionID]
	var size int64
	for _, e := range events {
		line, err := json.Marshal(e)
		if err != nil {
			return Meta{}, err
		}
		size += int64(len(line)) + 1
	}
	return Meta{SessionID: sessionID, Exists: len(events) > 0, SizeBytes: size}, nil
}

// GetEntries returns paginated events with the same offset/tail semantics as
// LocalFileStore.
func (s *InMemoryStore) GetEntries(sessionID string, q Query) ([]Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	all := s.events[sessionID]
	if q.Event != "" {
		filtered := make([]Event, 0, len(all))
		for _, e := range all {
			if e["event"] == q.Event {
				filtered = append(filtered, e)
			}
		}
		all = filtered
	}
	limit := normalizeLimit(q.Limit)
	if q.Offset != nil {
		// A negative offset skips nothing, matching the file store's
		// skipped-counter behavior.
		start := min(max(0, *q.Offset), len(all))
		end := min(start+limit, len(all))
		return append([]Event{}, all[start:end]...), nil
	}
	start := max(0, len(all)-limit)
	return append([]Event{}, all[start:]...), nil
}

// GetPath always returns "" — there is no local file.
func (s *InMemoryStore) GetPath(string) (string, error) {
	return "", nil
}

// ---------------------------------------------------------------------------
// NullStore
// ---------------------------------------------------------------------------

// NullStore silently discards all writes and returns empty results. Use it
// when recording is disabled to keep the Store interface consistent without
// nil checks in calling code.
type NullStore struct{}

// StartSession is a no-op.
func (NullStore) StartSession(string, map[string]any) error { return nil }

// AppendEvents is a no-op.
func (NullStore) AppendEvents(string, []Event) error { return nil }

// EndSession is a no-op.
func (NullStore) EndSession(string) error { return nil }

// RecordingMeta reports a non-existent recording.
func (NullStore) RecordingMeta(sessionID string) (Meta, error) {
	return Meta{SessionID: sessionID}, nil
}

// GetEntries returns no events.
func (NullStore) GetEntries(string, Query) ([]Event, error) { return []Event{}, nil }

// GetPath returns "".
func (NullStore) GetPath(string) (string, error) { return "", nil }
