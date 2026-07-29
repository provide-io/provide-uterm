//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"reflect"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
)

// A hosted session is only leasable because the hub can reach through it: pause
// it, read its screen, and put a lease holder's keystrokes into it. These are
// the four things the hub asks of a worker, held against the adapter that
// answers them.

func TestSessionWorkerPausesAndResumesTheConnector(t *testing.T) {
	conn := newFakeConnector()
	w := &sessionWorker{conn: conn}

	if err := w.SetHijacked(context.Background(), true); err != nil {
		t.Fatalf("SetHijacked(true): %v", err)
	}
	if err := w.SetHijacked(context.Background(), false); err != nil {
		t.Fatalf("SetHijacked(false): %v", err)
	}
	if err := w.RequestStep(context.Background()); err != nil {
		t.Fatalf("RequestStep: %v", err)
	}
	want := []string{"pause", "resume", "step"}
	if got := conn.controlActions(); !reflect.DeepEqual(got, want) {
		t.Fatalf("control actions = %v, want %v — a hijack is a pause on the worker", got, want)
	}
}

func TestSessionWorkerForwardsKeystrokesToTheTerminal(t *testing.T) {
	conn := newFakeConnector()
	w := &sessionWorker{conn: conn}

	if err := w.Send(context.Background(), "ls\r"); err != nil {
		t.Fatalf("Send: %v", err)
	}
	sent := conn.wire.sentStrings()
	if len(sent) != 1 || sent[0] != "ls\r" {
		t.Fatalf("wire saw %q, want the keystrokes the lease holder typed", sent)
	}
	// The resize the hub can send has nowhere to go on this Connector, and
	// says so by succeeding rather than by looking like a broken worker.
	if err := w.SetSize(context.Background(), 120, 40); err != nil {
		t.Fatalf("SetSize: %v", err)
	}
}

func TestSessionWorkerReportsTheScreen(t *testing.T) {
	conn := newFakeConnector()
	w := &sessionWorker{conn: conn}

	if w.Session() != bridge.Session(w) {
		t.Fatal("Session must report the adapter itself, so the bridge attaches one watch")
	}
	snap := w.Snapshot()
	if snap == nil {
		t.Fatal("Snapshot returned nil for a live session")
	}
	for _, key := range []string{"screen", "cols", "rows", "cursor"} {
		if _, ok := snap[key]; !ok {
			t.Fatalf("snapshot has no %q: %v", key, snap)
		}
	}
}

func TestSessionWorkerObservesTerminalOutput(t *testing.T) {
	conn := newFakeConnector()
	w := &sessionWorker{conn: conn}

	seen := make(chan []byte, 4)
	w.AddWatch(func(_ map[string]any, raw []byte) { seen <- raw })
	conn.wire.inbound <- []byte("hello")

	select {
	case raw := <-seen:
		if string(raw) != "hello" {
			t.Fatalf("watch saw %q, want the raw output", raw)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the bridge's watch never saw the terminal's output")
	}
}

// A stopped session is not a worker. Everything the hub would ask of it has to
// answer that way rather than pretending, or the hub would broadcast an empty
// screen as if it were the terminal's.
func TestSessionWorkerWithoutALiveSessionIsNotAWorker(t *testing.T) {
	conn := newFakeConnector()
	if err := conn.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	w := &sessionWorker{conn: conn}

	if w.Session() != nil {
		t.Fatal("Session must be nil once the connector has none")
	}
	if snap := w.Snapshot(); snap != nil {
		t.Fatalf("Snapshot = %v, want nil with no session", snap)
	}
	// Registering a watch on nothing is a no-op, not a panic.
	w.AddWatch(func(map[string]any, []byte) { t.Error("watch fired with no session") })
}
