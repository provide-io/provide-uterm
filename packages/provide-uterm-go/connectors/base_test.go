//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestBaseStartStopHandleInput(t *testing.T) {
	ctx := context.Background()
	c, ft := newFakeConnector("open")

	if c.IsConnected() {
		t.Fatal("connector should not be connected before Start")
	}
	if c.Session() != nil {
		t.Fatal("Session should be nil before Start")
	}
	// Snapshot before Start returns a zero value.
	if snap := c.Snapshot(); snap.Screen != "" {
		t.Fatalf("expected empty snapshot before start, got %q", snap.Screen)
	}

	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if !c.IsConnected() {
		t.Fatal("connector should be connected after Start")
	}
	if c.Session() == nil {
		t.Fatal("Session should be non-nil after Start")
	}
	// Start again is idempotent while connected.
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start idempotent: %v", err)
	}

	if err := c.HandleInput(ctx, "hello"); err != nil {
		t.Fatalf("HandleInput: %v", err)
	}
	if ft.sentCount() == 0 {
		t.Fatal("HandleInput should have sent bytes upstream")
	}

	if err := c.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if c.IsConnected() {
		t.Fatal("connector should be disconnected after Stop")
	}
	// Stop is idempotent.
	if err := c.Stop(ctx); err != nil {
		t.Fatalf("Stop idempotent: %v", err)
	}
	// HandleInput after Stop is a no-op (not connected).
	if err := c.HandleInput(ctx, "ignored"); err != nil {
		t.Fatalf("HandleInput after stop: %v", err)
	}
}

func TestBaseEventsCapturesRawOutput(t *testing.T) {
	ctx := context.Background()
	c, ft := newFakeConnector("open")
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = c.Stop(ctx) }()

	ft.inbound <- []byte("banner text\r\n")
	// Wait for the reader goroutine to observe the chunk.
	if _, err := c.Session().WaitForUpdate(ctx, time.Second); err != nil {
		t.Fatalf("WaitForUpdate: %v", err)
	}

	events := c.Events()
	if len(events) == 0 {
		t.Fatal("expected a buffered term event")
	}
	last := events[len(events)-1]
	if last["type"] != "term" {
		t.Fatalf("expected type=term, got %v", last["type"])
	}
	if data, _ := last["data"].(string); !strings.Contains(data, "banner text") {
		t.Fatalf("event missing raw output: %q", data)
	}
}

func TestBaseSetModeControlClearAnalysis(t *testing.T) {
	ctx := context.Background()
	c, _ := newFakeConnector("hijack")
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = c.Stop(ctx) }()

	// HandleControl transitions.
	for _, action := range []string{"pause", "resume", "step", "unknown"} {
		if err := c.HandleControl(action); err != nil {
			t.Fatalf("HandleControl(%s): %v", action, err)
		}
	}

	// pause then set open clears paused.
	_ = c.HandleControl("pause")
	if err := c.SetMode("open"); err != nil {
		t.Fatalf("SetMode open: %v", err)
	}
	c.mu.Lock()
	paused := c.paused
	mode := c.inputMode
	c.mu.Unlock()
	if paused || mode != "open" {
		t.Fatalf("SetMode(open) should clear paused; paused=%v mode=%s", paused, mode)
	}
	if err := c.SetMode("hijack"); err != nil {
		t.Fatalf("SetMode hijack: %v", err)
	}
	if err := c.SetMode("bogus"); err == nil {
		t.Fatal("SetMode should reject invalid mode")
	}

	// Clear resets buffered events and drives the emulator; the fake transport is
	// idle (reader parked in Receive), so the emulator access does not race.
	if err := c.Clear(); err != nil {
		t.Fatalf("Clear: %v", err)
	}
	if len(c.Events()) != 0 {
		t.Fatal("Clear should drop buffered events")
	}

	analysis := c.Analysis()
	for _, want := range []string{"fake-sess", "input_mode", "bytes_received", "connected"} {
		if !strings.Contains(analysis, want) {
			t.Fatalf("analysis missing %q: %s", want, analysis)
		}
	}
}

func TestOnRawRingCap(t *testing.T) {
	c, _ := newFakeConnector("open")
	for i := 0; i < maxEvents+50; i++ {
		c.onRaw(nil, []byte("x"))
	}
	if got := len(c.Events()); got != maxEvents {
		t.Fatalf("event ring should cap at %d, got %d", maxEvents, got)
	}
}

func TestBaseStartDialError(t *testing.T) {
	// A build whose Connect fails surfaces the error from Start and leaves the
	// connector disconnected.
	c, ft := newFakeConnector("open")
	ft.connectErr = errBoom
	if err := c.Start(context.Background()); err == nil {
		t.Fatal("Start should surface the dial error")
	}
	if c.IsConnected() {
		t.Fatal("connector must not be connected after a failed Start")
	}
}
