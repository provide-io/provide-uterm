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

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
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

// A create whose id is still being torn down waits for the teardown, then
// proceeds: a tty-named PAM id is reused by the next login on that tty.
func TestCreateWaitsForAnIDBeingDeleted(t *testing.T) {
	store := &gatedFileStore{
		LocalFileStore: recording.NewLocalFileStore(t.TempDir()),
		retire:         make(chan struct{}), release: make(chan struct{}),
	}
	r := newTestRegistry(t)
	r.SetRecording(recording.NewInMemoryStore(), nil)
	must(t, createRetireSession(t, r, retireID))
	r.SetRecording(store, nil)

	deleted := make(chan error, 1)
	go func() { deleted <- r.DeleteSession(context.Background(), retireID) }()
	waitRetire(t, store) // the delete is retiring the recording
	created := make(chan error, 1)
	go func() { created <- createRetireSession(t, r, retireID) }()
	select {
	case err := <-created:
		t.Fatalf("create returned %v while the id was still being deleted", err)
	case <-time.After(50 * time.Millisecond):
	}
	close(store.release)
	must(t, <-deleted)
	waitRetire(t, store) // the create's own retire of any leftover
	must(t, <-created)
	readNothing(t, store)
}

func waitRetire(t *testing.T, store *gatedFileStore) {
	t.Helper()
	select {
	case <-store.retire:
	case <-time.After(5 * time.Second):
		t.Fatal("the recording was never retired")
	}
}

// Only a teardown that outlasts the wait is refused.
func TestCreateThatOutwaitsTheBoundIsRefused(t *testing.T) {
	saved := reservedIDWait
	t.Cleanup(func() { reservedIDWait = saved })
	reservedIDWait = 50 * time.Millisecond
	store := &gatedFileStore{
		LocalFileStore: recording.NewLocalFileStore(t.TempDir()),
		retire:         make(chan struct{}), release: make(chan struct{}),
	}
	r := newTestRegistry(t)
	r.SetRecording(recording.NewInMemoryStore(), nil)
	must(t, createRetireSession(t, r, retireID))
	r.SetRecording(store, nil)

	deleted := make(chan error, 1)
	go func() { deleted <- r.DeleteSession(context.Background(), retireID) }()
	waitRetire(t, store)
	err := createRetireSession(t, r, retireID)
	var conflict *server.SessionConflictError
	if !errors.As(err, &conflict) || !strings.Contains(conflict.Msg, "being deleted") {
		t.Fatalf("create past the bound = %v, want a being-deleted conflict", err)
	}
	close(store.release)
	must(t, <-deleted)
}

type slowRetireStore struct{ *recording.InMemoryStore }

func (s slowRetireStore) RetireSession(id string) error {
	time.Sleep(20 * time.Millisecond) // the first create is still retiring when the retries arrive
	return s.InMemoryStore.RetireSession(id)
}

// The relay retries an open: the losers get "already exists", which the route
// treats as idempotent, never "being deleted".
func TestConcurrentCreatesOfOneIDSeeAlreadyExists(t *testing.T) {
	r := newTestRegistry(t)
	r.SetRecording(slowRetireStore{recording.NewInMemoryStore()}, nil)
	errs := make(chan error, 3)
	for range 3 {
		go func() { errs <- createRetireSession(t, r, retireID) }()
	}
	var ok, exists int
	for range 3 {
		err := <-errs
		var conflict *server.SessionConflictError
		switch {
		case err == nil:
			ok++
		case errors.As(err, &conflict) && strings.Contains(conflict.Msg, "already exists"):
			exists++
		default:
			t.Fatalf("create = %v", err)
		}
	}
	if ok != 1 || exists != 2 {
		t.Fatalf("ok=%d exists=%d, want 1 and 2", ok, exists)
	}
}

// stuckConnector's Stop hangs until released, or panics.
type stuckConnector struct {
	*fakeConnector
	panics  bool
	release chan struct{}
}

func (c *stuckConnector) Stop(context.Context) error {
	if c.panics {
		panic("connector stop exploded")
	}
	<-c.release
	return nil
}

func startedWith(t *testing.T, conn connectors.Connector) *SessionRegistryImpl {
	t.Helper()
	r := newTestRegistry(t)
	r.connect = func(context.Context, serverconfig.SessionDefinition) (connectors.Connector, error) { return conn, nil }
	r.SetRecording(recording.NewInMemoryStore(), nil)
	must(t, createRetireSession(t, r, retireID))
	if _, err := r.StartSession(context.Background(), retireID); err != nil {
		t.Fatal(err)
	}
	return r
}

// A stop that hangs or panics cannot hold the id: the delete retires and
// releases it anyway.
func TestHungOrPanickingStopStillReleasesTheID(t *testing.T) {
	saved := removalStopTimeout
	t.Cleanup(func() { removalStopTimeout = saved })
	removalStopTimeout = 50 * time.Millisecond
	for name, conn := range map[string]*stuckConnector{
		"hang":  {fakeConnector: newFakeConnector(), release: make(chan struct{})},
		"panic": {fakeConnector: newFakeConnector(), panics: true},
	} {
		t.Run(name, func(t *testing.T) {
			r := startedWith(t, conn)
			deleted := make(chan error, 1)
			go func() { deleted <- r.DeleteSession(context.Background(), retireID) }()
			select {
			case err := <-deleted:
				must(t, err)
			case <-time.After(5 * time.Second):
				t.Fatal("DeleteSession is still waiting on the stop")
			}
			must(t, createRetireSession(t, r, retireID))
			if conn.release != nil {
				close(conn.release)
			}
		})
	}
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
