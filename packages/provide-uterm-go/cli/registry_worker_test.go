//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// hubLinkedRegistry is a registry wired to a real hub, with fake connectors and
// a manager URL nothing is listening on: the bridges it builds are real and
// really try to dial, which is what these tests are about — that a started
// session builds one at all, and only one.
func hubLinkedRegistry(t *testing.T) (*SessionRegistryImpl, *hub.TermHub) {
	t.Helper()
	r := newTestRegistry(t)
	h := hub.NewTermHub(hub.TermHubConfig{})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	r.SetHubLink(ctx, h, "http://127.0.0.1:1", "")
	t.Cleanup(func() { _, _ = r.StopSession(context.Background(), "provide-shell") })
	return r, h
}

// A started session gets exactly one worker bridge, and a second start does not
// stack another on top: two bridges for one session would mean two sockets
// racing to be the worker the hub pauses.
func TestStartingASessionAttachesOneWorkerBridge(t *testing.T) {
	r, _ := hubLinkedRegistry(t)
	ctx := context.Background()

	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("StartSession: %v", err)
	}
	r.mu.Lock()
	first := r.entries["provide-shell"].bridge
	r.mu.Unlock()
	if first == nil {
		t.Fatal("a started session must attach a worker bridge")
	}

	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("StartSession again: %v", err)
	}
	r.mu.Lock()
	second := r.entries["provide-shell"].bridge
	// StartSession returns early for a session that is already connected, so
	// the attach itself is asked directly here: a restart that got as far as a
	// second attach must still leave one bridge, not two sockets racing to be
	// the worker.
	r.startWorkerBridge(r.entries["provide-shell"])
	third := r.entries["provide-shell"].bridge
	r.mu.Unlock()
	if second != first || third != first {
		t.Fatal("attaching again replaced the worker bridge instead of keeping it")
	}
}

// Stopping a session takes its worker away. A bridge that outlived its
// connector would keep the session looking leasable after its terminal was
// gone.
func TestStoppingASessionDetachesTheWorkerBridge(t *testing.T) {
	r, _ := hubLinkedRegistry(t)
	ctx := context.Background()

	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("StartSession: %v", err)
	}
	if _, err := r.StopSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("StopSession: %v", err)
	}
	r.mu.Lock()
	br := r.entries["provide-shell"].bridge
	r.mu.Unlock()
	if br != nil {
		t.Fatal("the worker bridge outlived the session it belonged to")
	}
}

// Deleting a session takes its worker away too. Deletion is the more final of
// the two teardowns, so a bridge left attached here reconnects forever against
// a session id the registry can no longer even list.
func TestDeletingASessionDetachesTheWorkerBridge(t *testing.T) {
	r, _ := hubLinkedRegistry(t)
	ctx := context.Background()

	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("StartSession: %v", err)
	}
	// The entry leaves the map on delete, so hold it to inspect afterwards.
	r.mu.Lock()
	e := r.entries["provide-shell"]
	r.mu.Unlock()
	if e.bridge == nil {
		t.Fatal("precondition: a started session must have a worker bridge")
	}

	if err := r.DeleteSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("DeleteSession: %v", err)
	}
	r.mu.Lock()
	br := e.bridge
	r.mu.Unlock()
	if br != nil {
		t.Fatal("the worker bridge outlived the session it belonged to")
	}
}

// Without a hub link the registry is the connector-only thing it was: a caller
// that never wired one (the CLI's non-server paths, and every registry unit
// test) gets a live connector and no worker.
func TestRegistryWithoutAHubLinkAttachesNoWorker(t *testing.T) {
	r := newTestRegistry(t)
	if _, err := r.StartSession(context.Background(), "provide-shell"); err != nil {
		t.Fatalf("StartSession: %v", err)
	}
	r.mu.Lock()
	br := r.entries["provide-shell"].bridge
	r.mu.Unlock()
	if br != nil {
		t.Fatal("a registry with no hub must not build a worker bridge")
	}
	// The mode sync is the same no-op, rather than a nil dereference.
	r.syncHubInputMode(context.Background(), "provide-shell", "open")
	if !r.WorkersAttached() {
		t.Fatal("with no hub, a running auto_start session is as attached as it gets")
	}
}

// WorkersAttached is what "ready" means to the live-conformance handshake: the
// auto_start sessions are up, the hub can reach them, AND each has announced the
// mode it is in. Any one of the three missing is not ready.
func TestWorkersAttachedNeedsBothHalves(t *testing.T) {
	r, h := hubLinkedRegistry(t)
	ctx := context.Background()
	// A session nobody asked to auto-start has no say in readiness: the
	// handshake waits for what the deployment declared it wanted running.
	if _, err := r.CreateSession(ctx, map[string]any{"session_id": "manual", "connector_type": "shell"}); err != nil {
		t.Fatalf("create: %v", err)
	}
	if r.WorkersAttached() {
		t.Fatal("a session that has not started is not attached")
	}
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("StartSession: %v", err)
	}
	if r.WorkersAttached() {
		t.Fatal("a running session whose worker never reached the hub is not attached")
	}
	// Standing in for the worker socket the bridge would have opened. A socket
	// on its own is the window this gate exists to close: it is attached, but
	// the hub still holds the "hijack" default rather than the mode the worker
	// booted in, so a lease taken now is granted against the wrong state.
	st := hub.NewWorkerTermState()
	st.WorkerWS = stubWorkerWS{}
	h.Registry.Put("provide-shell", st)
	if r.WorkersAttached() {
		t.Fatal("a worker whose hello has not landed is not attached")
	}
	if _, err := h.SetWorkerHello(ctx, "provide-shell", hub.InputModeOpen, nil); err != nil {
		t.Fatalf("SetWorkerHello: %v", err)
	}
	if !r.WorkersAttached() {
		t.Fatal("a running session with a worker socket and a landed hello is attached")
	}
}

// stubWorkerWS stands in for an attached worker socket.
type stubWorkerWS struct{}

func (stubWorkerWS) SendText(context.Context, string) error { return nil }

// A deployment that sets a worker bearer token has its hosted sessions present
// it: without the header the hub closes their handshake, and the server would
// host sessions it could never lease.
func TestWorkerBearerTokenIsReadFromConfig(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	if got := workerBearerToken(cfg); got != "" {
		t.Fatalf("workerBearerToken = %q, want empty when none is configured", got)
	}
	token := "s3cret" //nolint:gosec // test fixture, not a credential
	cfg.Auth.WorkerBearerToken = &token
	if got := workerBearerToken(cfg); got != token {
		t.Fatalf("workerBearerToken = %q, want %q", got, token)
	}
}
