//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// registryWithConn builds a registry whose connect hook returns conn, and starts
// the "provide-shell" session so an entry with a live connector exists.
func registryWithConn(t *testing.T, conn connectors.Connector) *SessionRegistryImpl {
	t.Helper()
	cfg := serverconfig.DefaultServerConfig()
	r := NewSessionRegistry(cfg)
	r.connect = func(context.Context, serverconfig.SessionDefinition) (connectors.Connector, error) {
		return conn, nil
	}
	if _, err := r.StartSession(context.Background(), "provide-shell"); err != nil {
		t.Fatalf("start: %v", err)
	}
	return r
}

// TestWatchSessionEventsNoBusClamps covers the timeout/maxEvents clamp branches
// and the no-bus connector-ring trim path.
func TestWatchSessionEventsNoBusClamps(t *testing.T) {
	r := registryWithConn(t, manyEventsConnector{newFakeConnector()})
	ctx := context.Background()

	// TimeoutMS below floor (->100) and MaxEvents above ceiling (->200).
	if _, err := r.WatchSessionEvents(ctx, "provide-shell", server.WatchParams{TimeoutMS: 50, MaxEvents: 300}); err != nil {
		t.Fatalf("clamp low/high: %v", err)
	}
	// TimeoutMS above ceiling (->30000), MaxEvents below floor (->50), and the
	// ring (3 events) exceeds MaxEvents=2 so the tail-trim branch runs.
	res, err := r.WatchSessionEvents(ctx, "provide-shell", server.WatchParams{TimeoutMS: 40000, MaxEvents: 2})
	if err != nil {
		t.Fatalf("clamp high + trim: %v", err)
	}
	events, _ := res["events"].([]map[string]any)
	if len(events) != 2 {
		t.Fatalf("expected trimmed to 2 events, got %d", len(events))
	}
	// MaxEvents<=0 defaults to 50.
	if _, err := r.WatchSessionEvents(ctx, "provide-shell", server.WatchParams{TimeoutMS: 100, MaxEvents: 0}); err != nil {
		t.Fatalf("maxEvents default: %v", err)
	}
}

// TestWatchSessionEventsBusBootstrap covers the EventBus bootstrap early-return
// (ring already fills MaxEvents) plus the pattern branch.
func TestWatchSessionEventsBusBootstrap(t *testing.T) {
	r := registryWithConn(t, manyEventsConnector{newFakeConnector()})
	r.SetEventBus(hub.NewEventBus(hub.EventBusOptions{}))

	res, err := r.WatchSessionEvents(context.Background(), "provide-shell", server.WatchParams{
		TimeoutMS: 1000,
		MaxEvents: 2,
		Pattern:   "n",
	})
	if err != nil {
		t.Fatalf("bootstrap: %v", err)
	}
	events, _ := res["events"].([]map[string]any)
	if len(events) != 2 {
		t.Fatalf("expected bootstrap to fill 2 events, got %d", len(events))
	}
	if res["timed_out"] != false {
		t.Fatalf("bootstrap early-return should not be timed_out")
	}
}

// TestWatchSessionEventsWatchError covers the bus.Watch error return (bad regex).
func TestWatchSessionEventsWatchError(t *testing.T) {
	r := registryWithConn(t, newFakeConnector())
	r.SetEventBus(hub.NewEventBus(hub.EventBusOptions{}))
	if _, err := r.WatchSessionEvents(context.Background(), "provide-shell", server.WatchParams{
		TimeoutMS: 1000,
		MaxEvents: 5,
		Pattern:   "[", // invalid regex -> compile error
	}); err == nil {
		t.Fatal("expected pattern compile error")
	}
}

// TestWatchSessionEventsContextCancel covers the ctx.Done branch of the poll loop.
func TestWatchSessionEventsContextCancel(t *testing.T) {
	r := registryWithConn(t, newFakeConnector()) // Events() is empty -> enters long-poll
	r.SetEventBus(hub.NewEventBus(hub.EventBusOptions{}))
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := r.WatchSessionEvents(ctx, "provide-shell", server.WatchParams{TimeoutMS: 5000, MaxEvents: 5}); err == nil {
		t.Fatal("expected context cancellation error")
	}
}

// TestWatchSessionEventsWorkerDisconnect covers the closed-queue / nil-sentinel
// branch: CloseWorker pushes a nil map onto the subscription queue.
func TestWatchSessionEventsWorkerDisconnect(t *testing.T) {
	r := registryWithConn(t, newFakeConnector())
	bus := hub.NewEventBus(hub.EventBusOptions{})
	r.SetEventBus(bus)

	go func() {
		time.Sleep(30 * time.Millisecond)
		bus.CloseWorker("provide-shell")
	}()
	res, err := r.WatchSessionEvents(context.Background(), "provide-shell", server.WatchParams{
		TimeoutMS: 3000,
		MaxEvents: 5,
	})
	if err != nil {
		t.Fatalf("worker disconnect: %v", err)
	}
	if res["timed_out"] != false {
		t.Fatalf("worker-disconnect sentinel should not be timed_out: %v", res)
	}
}
