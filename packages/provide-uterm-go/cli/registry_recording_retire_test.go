//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
)

// A deleted session's recording is never served to the id's next holder: not
// before it starts recording, and not if it never records. DeleteSession
// retires the recording after the session's last writes have landed, and
// CreateSession retires any leftover; the id stays reserved meanwhile. Port of
// test_registry_recording_retire.py.

const retireID = "pam-suokki-capture-4242-777"

func createRetireSession(t *testing.T, r *SessionRegistryImpl, id string) error {
	t.Helper()
	_, err := r.CreateSession(context.Background(), map[string]any{"session_id": id, "connector_type": "shell"})
	return err
}

func recordAs(t *testing.T, store recording.Store, text string) {
	t.Helper()
	must(t, store.StartSession(retireID, map[string]any{"instance": "a"}))
	must(t, store.AppendEvents(retireID, []recording.Event{{"ts": 1.0, "event": "read", "data": map[string]any{"text": text}}}))
	must(t, store.EndSession(retireID))
}

func readNothing(t *testing.T, store recording.Store) {
	t.Helper()
	got, err := store.GetEntries(retireID, recording.Query{Limit: 500})
	must(t, err)
	if len(got) != 0 {
		t.Fatalf("the new session was served %v", got)
	}
	meta, err := store.RecordingMeta(retireID)
	must(t, err)
	if meta.Exists {
		t.Fatalf("meta = %+v, want no recording", meta)
	}
}

func TestRecreatedIDReadsNothingOfTheDeletedSession(t *testing.T) {
	for name, store := range map[string]recording.Store{
		"file":   recording.NewLocalFileStore(t.TempDir()),
		"memory": recording.NewInMemoryStore(),
	} {
		t.Run(name, func(t *testing.T) {
			r := newTestRegistry(t)
			r.SetRecording(store, nil)
			must(t, createRetireSession(t, r, retireID))
			recordAs(t, store, "secretA")
			must(t, r.DeleteSession(context.Background(), retireID))
			must(t, createRetireSession(t, r, retireID))
			readNothing(t, store) // before any start: it may never start
		})
	}
}

func TestLeftoverRecordingIsRetiredWhenItsIDIsCreated(t *testing.T) {
	store := recording.NewInMemoryStore()
	recordAs(t, store, "secret-from-before-restart")
	r := newTestRegistry(t)
	r.SetRecording(store, nil)
	must(t, createRetireSession(t, r, retireID))
	readNothing(t, store)
}

// gatedFileStore refuses writes while refuse is set, and holds RetireSession
// until release is closed (when it is non-nil).
type gatedFileStore struct {
	*recording.LocalFileStore
	mu      sync.Mutex
	refuse  bool
	retire  chan struct{}
	release chan struct{}
}

func (g *gatedFileStore) refusing() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.refuse
}

func (g *gatedFileStore) setRefuse(v bool) {
	g.mu.Lock()
	g.refuse = v
	g.mu.Unlock()
}

func (g *gatedFileStore) AppendEvents(id string, events []recording.Event) error {
	if g.refusing() {
		return errors.New("disk full")
	}
	return g.LocalFileStore.AppendEvents(id, events)
}

func (g *gatedFileStore) EndSession(id string) error {
	if g.refusing() {
		return errors.New("disk full")
	}
	return g.LocalFileStore.EndSession(id)
}

func (g *gatedFileStore) RetireSession(id string) error {
	if g.release != nil {
		g.retire <- struct{}{}
		<-g.release
	}
	return g.LocalFileStore.RetireSession(id)
}

// The old session's last writes (here, a recording its last connection could
// not close, settled by DeleteSession) land in its own recording, which is then
// retired; the new session never sees them.
func TestDeletedSessionsLastWritesLandBeforeRetirement(t *testing.T) {
	dir := t.TempDir()
	store := &gatedFileStore{LocalFileStore: recording.NewLocalFileStore(dir)}
	r := newTestRegistry(t)
	r.SetRecording(store, nil)
	must(t, createRetireSession(t, r, retireID))
	r.mu.Lock()
	rec := r.recorderFor(r.entries[retireID])
	r.mu.Unlock()
	rec.setEnabled(true)
	rec.AttemptStarted()
	store.setRefuse(true)
	_ = rec.InputReceived("late-A")
	rec.AttemptEnded(nil) // cannot close: kept unclosed for settle
	store.setRefuse(false)

	must(t, r.DeleteSession(context.Background(), retireID))
	must(t, createRetireSession(t, r, retireID))
	readNothing(t, store)
	retired, _ := filepath.Glob(filepath.Join(dir, retireID+".*.jsonl"))
	if len(retired) != 1 {
		t.Fatalf("retired = %v, want one", retired)
	}
	raw, _ := os.ReadFile(retired[0])
	if !strings.Contains(string(raw), "late-A") || !strings.Contains(string(raw), "log_stop") {
		t.Fatalf("the old session's last writes are not in its retired recording: %s", raw)
	}
}

func TestCreateUnderAnIDBeingDeletedIsRefused(t *testing.T) {
	store := &gatedFileStore{
		LocalFileStore: recording.NewLocalFileStore(t.TempDir()),
		retire:         make(chan struct{}), release: make(chan struct{}),
	}
	r := newTestRegistry(t)
	r.SetRecording(recording.NewInMemoryStore(), nil)
	must(t, createRetireSession(t, r, retireID))
	r.SetRecording(store, nil)

	done := make(chan error, 1)
	go func() { done <- r.DeleteSession(context.Background(), retireID) }()
	select { // the delete is retiring the recording
	case <-store.retire:
	case <-time.After(5 * time.Second):
		t.Fatal("DeleteSession never retired the recording")
	}
	err := createRetireSession(t, r, retireID)
	var conflict *server.SessionConflictError
	if !errors.As(err, &conflict) || !strings.Contains(conflict.Msg, "being deleted") {
		t.Fatalf("create during delete = %v, want a being-deleted conflict", err)
	}
	close(store.release)
	must(t, <-done)
	store.release = nil
	must(t, createRetireSession(t, r, retireID))
}

func TestSessionIDsAreCappedSoEveryRecordingCanBeRetired(t *testing.T) {
	r := newTestRegistry(t)
	must(t, createRetireSession(t, r, strings.Repeat("a", 200)))
	var invalid *server.SessionValidationError
	if err := createRetireSession(t, r, strings.Repeat("a", 201)); !errors.As(err, &invalid) {
		t.Fatalf("a 201-byte id = %v, want a validation error", err)
	}
}

func TestTheLongestIDCanBeRetiredOnDisk(t *testing.T) {
	dir := t.TempDir()
	store := recording.NewLocalFileStore(dir)
	id := strings.Repeat("a", 200)
	must(t, store.StartSession(id, map[string]any{"instance": "a"}))
	must(t, store.EndSession(id))
	must(t, recording.Retire(store, id))
	if got, _ := filepath.Glob(filepath.Join(dir, id+".*.jsonl")); len(got) != 1 {
		t.Fatalf("retired = %v", got)
	}
}
