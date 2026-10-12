//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"errors"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
)

// How a create's or a delete's reservation of an id ends when the call does
// not get as far as the insert: every way out releases it.

var errRetireRefused = errors.New("retire refused")

// retireFailStore fails, or panics in, RetireSession.
type retireFailStore struct {
	*recording.InMemoryStore
	panics bool
}

func (s retireFailStore) RetireSession(string) error {
	if s.panics {
		panic("retire exploded")
	}
	return errRetireRefused
}

func reserved(r *SessionRegistryImpl, id string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	_, held := r.retiring[id]
	return held
}

// A create whose leftover recording cannot be retired fails without creating
// the session, and the id is free for the next try.
func TestCreateFailsWhenTheLeftoverCannotBeRetired(t *testing.T) {
	r := newTestRegistry(t)
	r.SetRecording(retireFailStore{InMemoryStore: recording.NewInMemoryStore()}, nil)
	if err := createRetireSession(t, r, retireID); !errors.Is(err, errRetireRefused) {
		t.Fatalf("create = %v, want the refused retire", err)
	}
	if _, err := r.GetSession(context.Background(), retireID); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("GetSession = %v, want not found", err)
	}
	if reserved(r, retireID) {
		t.Fatal("the id is still reserved")
	}
	r.SetRecording(recording.NewInMemoryStore(), nil)
	must(t, createRetireSession(t, r, retireID))
}

// A panic while the leftover is retired still releases the id.
func TestAPanicWhileCreatingStillReleasesTheID(t *testing.T) {
	r := newTestRegistry(t)
	r.SetRecording(retireFailStore{InMemoryStore: recording.NewInMemoryStore(), panics: true}, nil)
	func() {
		defer func() {
			if recover() == nil {
				t.Fatal("expected the planted panic")
			}
		}()
		_ = createRetireSession(t, r, retireID)
	}()
	if reserved(r, retireID) {
		t.Fatal("the id is still reserved")
	}
}

// A create waiting on a held id gives up when its caller does.
func TestCreateWaitingOnAHeldIDHonoursItsContext(t *testing.T) {
	r := newTestRegistry(t)
	r.mu.Lock()
	r.retiring[retireID] = make(chan struct{}) // a teardown that never ends
	r.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := r.CreateSession(ctx, map[string]any{"session_id": retireID, "connector_type": "shell"})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("create = %v, want context.Canceled", err)
	}
}

// A second delete of an id already being torn down returns at once and leaves
// the teardown, and the retirement, to the first.
func TestASecondDeleteLeavesTheTeardownToTheFirst(t *testing.T) {
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
	waitRetire(t, store) // the first delete holds the id, retiring
	must(t, r.DeleteSession(context.Background(), retireID))
	if !reserved(r, retireID) {
		t.Fatal("the second delete released the first one's reservation")
	}
	close(store.release)
	must(t, <-deleted)
	if reserved(r, retireID) {
		t.Fatal("the id is still reserved after the first delete")
	}
}
