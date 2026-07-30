//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"testing"
)

// A worker_hello announces what the worker process booted with; SetInputMode is
// a decision made through an authenticated route. The hub has to tell them
// apart, because InputMode defaults to hijack: a rule refusing every hello that
// lowers hijack to open would refuse every worker that legitimately announces
// open.
func withWorker(t *testing.T) (*TermHub, string) {
	t.Helper()
	h := NewTermHub(TermHubConfig{})
	id := "w-hello"
	h.lock.Lock()
	h.registry.Put(id, NewWorkerTermState())
	h.lock.Unlock()
	return h, id
}

func TestHelloMaySetTheModeWhenNobodyHasDecided(t *testing.T) {
	h, id := withWorker(t)

	ok, err := h.Conn.SetWorkerHello(context.Background(), id, InputModeOpen, nil)
	if err != nil || !ok {
		t.Fatalf("hello on an undecided session must apply: ok=%v err=%v", ok, err)
	}
	if got := h.registry.Get(id).InputMode; got != InputModeOpen {
		t.Fatalf("input mode = %q, want %q", got, InputModeOpen)
	}
}

func TestHelloCannotUndoADecision(t *testing.T) {
	h, id := withWorker(t)
	if ok, _, err := h.Router.SetInputMode(context.Background(), id, InputModeHijack); err != nil || !ok {
		t.Fatalf("operator set hijack: ok=%v err=%v", ok, err)
	}

	ok, err := h.Conn.SetWorkerHello(context.Background(), id, InputModeOpen, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if ok {
		t.Fatal("a hello must not lower a decided hijack to open")
	}
	if got := h.registry.Get(id).InputMode; got != InputModeHijack {
		t.Fatalf("input mode = %q, want it unchanged at %q", got, InputModeHijack)
	}
}

func TestHelloMayStillRaiseOverADecision(t *testing.T) {
	// One-directional: a worker announcing hijack tells the hub something it does
	// not otherwise know, that automation is driving the session.
	h, id := withWorker(t)
	if ok, _, err := h.Router.SetInputMode(context.Background(), id, InputModeOpen); err != nil || !ok {
		t.Fatalf("operator set open: ok=%v err=%v", ok, err)
	}

	ok, err := h.Conn.SetWorkerHello(context.Background(), id, InputModeHijack, nil)
	if err != nil || !ok {
		t.Fatalf("raising must be allowed: ok=%v err=%v", ok, err)
	}
	if got := h.registry.Get(id).InputMode; got != InputModeHijack {
		t.Fatalf("input mode = %q, want %q", got, InputModeHijack)
	}
}

func TestHelloAgreeingWithADecidedOpenIsNotADowngrade(t *testing.T) {
	h, id := withWorker(t)
	if ok, _, err := h.Router.SetInputMode(context.Background(), id, InputModeOpen); err != nil || !ok {
		t.Fatalf("operator set open: ok=%v err=%v", ok, err)
	}

	ok, err := h.Conn.SetWorkerHello(context.Background(), id, InputModeOpen, nil)
	if err != nil || !ok {
		t.Fatalf("a hello agreeing with the decision must apply: ok=%v err=%v", ok, err)
	}
}

func TestADecisionSurvivesRepeatedReconnects(t *testing.T) {
	// Why the flag lives on the worker state rather than the connection: registry
	// state outlives a worker socket.
	h, id := withWorker(t)
	if ok, _, err := h.Router.SetInputMode(context.Background(), id, InputModeHijack); err != nil || !ok {
		t.Fatalf("operator set hijack: ok=%v err=%v", ok, err)
	}

	for attempt := 0; attempt < 3; attempt++ {
		ok, err := h.Conn.SetWorkerHello(context.Background(), id, InputModeOpen, nil)
		if err != nil {
			t.Fatalf("attempt %d: unexpected error: %v", attempt, err)
		}
		if ok {
			t.Fatalf("attempt %d: reconnect must not undo the decision", attempt)
		}
	}
	if got := h.registry.Get(id).InputMode; got != InputModeHijack {
		t.Fatalf("input mode = %q, want %q", got, InputModeHijack)
	}
}
