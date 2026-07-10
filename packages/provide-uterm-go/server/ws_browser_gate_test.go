//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// recWorkerWS records payloads forwarded to the worker (a stand-in worker socket
// implementing hub.WorkerWS).
type recWorkerWS struct {
	mu   sync.Mutex
	sent []string
}

func (w *recWorkerWS) SendText(_ context.Context, payload string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.sent = append(w.sent, payload)
	return nil
}

func (w *recWorkerWS) count() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.sent)
}

// gateFixture registers a hijack-mode worker whose input forwarding can be
// observed, returning the server, the recording worker socket, and the browser
// conn used as the input source.
func gateFixture(t *testing.T) (*testServer, *recWorkerWS, *browserConn, *hub.WorkerTermState) {
	t.Helper()
	ts := newTestServer(t, nil)
	worker := &recWorkerWS{}
	bc := &browserConn{}
	st := hub.NewWorkerTermState()
	st.InputMode = hub.InputModeHijack
	st.WorkerWS = worker
	st.Browsers[bc] = "admin"
	ts.hub.Registry.Put("w", st)
	return ts, worker, bc, st
}

// TestBrowserInputGateForwardsForOwner verifies an owner within limits forwards:
// the hijack owner's keystroke reaches the worker.
func TestBrowserInputGateForwardsForOwner(t *testing.T) {
	ts, worker, bc, st := gateFixture(t)
	// bc owns an unexpired dashboard lease (nil ExpiresAt == active).
	st.HijackOwner = bc

	ts.srv.sendBrowserInput(context.Background(), "w", bc, "ls\n")
	if worker.count() == 0 {
		t.Fatal("owner input should be forwarded to the worker")
	}
}

// TestBrowserInputGateDropsNonOwner verifies a non-owner input is dropped by the
// gate (no hijack owner in hijack mode -> cannot send).
func TestBrowserInputGateDropsNonOwner(t *testing.T) {
	ts, worker, bc, _ := gateFixture(t)
	// No hijack owner set -> the lease is inactive -> bc cannot send.

	ts.srv.sendBrowserInput(context.Background(), "w", bc, "whoami\n")
	if worker.count() != 0 {
		t.Fatal("non-owner input must be dropped, not forwarded")
	}
}

// TestBrowserInputGateDropsExpiredLease verifies an expired-lease owner is
// dropped by the gate.
func TestBrowserInputGateDropsExpiredLease(t *testing.T) {
	ts, worker, bc, st := gateFixture(t)
	st.HijackOwner = bc
	expired := -1.0 // strictly less than the real clock's monotonic reading (>= 0)
	st.HijackOwnerExpiresAt = &expired

	ts.srv.sendBrowserInput(context.Background(), "w", bc, "rm -rf\n")
	if worker.count() != 0 {
		t.Fatal("expired-lease input must be dropped, not forwarded")
	}
}
