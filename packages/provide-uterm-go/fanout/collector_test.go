//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"context"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

func waitForSub(bus *hub.EventBus, workerID string) bool {
	deadline := time.Now().Add(2 * time.Second)
	for bus.SubscriberCount(workerID) < 1 {
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(time.Millisecond)
	}
	return true
}

func TestCollectorNoEventBus(t *testing.T) {
	d, e := OutputCollector{}.Collect(context.Background(), nil, "w1", 100, 1000)
	if d != "" || e != 0 {
		t.Fatalf("no-bus Collect = (%q,%d), want (\"\",0)", d, e)
	}
}

func TestCollectorCapturesTermEvents(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	go waitAndEmitTerm(bus, "w1", "hello ", "world")
	d, e := OutputCollector{}.Collect(context.Background(), bus, "w1", 200, 5000)
	if d != "hello world" {
		t.Fatalf("delta = %q, want %q", d, "hello world")
	}
	if e < 0 {
		t.Fatalf("elapsed = %d", e)
	}
}

func TestCollectorQuiescesBeforeMax(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	go waitAndEmitTerm(bus, "w1", "ping")
	start := time.Now()
	d, _ := OutputCollector{}.Collect(context.Background(), bus, "w1", 100, 10000)
	if d != "ping" {
		t.Fatalf("delta = %q", d)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("did not quiesce well before max")
	}
}

func TestCollectorSnapshotFallback(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	go func() {
		if !waitForSub(bus, "w1") {
			return
		}
		bus.Enqueue("w1", map[string]any{"type": "snapshot", "data": map[string]any{"screen": "screen-text"}})
	}()
	d, _ := OutputCollector{}.Collect(context.Background(), bus, "w1", 80, 5000)
	if d != "screen-text" {
		t.Fatalf("snapshot fallback delta = %q, want screen-text", d)
	}
}

func TestCollectorTermPriorityOverSnapshot(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	go func() {
		if !waitForSub(bus, "w1") {
			return
		}
		bus.Enqueue("w1", map[string]any{"type": "snapshot", "data": map[string]any{"screen": "ignored"}})
		bus.Enqueue("w1", map[string]any{"type": "term", "data": map[string]any{"data": "captured"}})
	}()
	d, _ := OutputCollector{}.Collect(context.Background(), bus, "w1", 80, 5000)
	if d != "captured" {
		t.Fatalf("delta = %q, want captured", d)
	}
}

func TestCollectorSkipsEmptyPayloads(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	go func() {
		if !waitForSub(bus, "w1") {
			return
		}
		bus.Enqueue("w1", map[string]any{"type": "term", "data": map[string]any{"data": ""}})
		bus.Enqueue("w1", map[string]any{"type": "snapshot", "data": map[string]any{"screen": ""}})
		bus.Enqueue("w1", map[string]any{"type": "term", "data": map[string]any{"data": "real"}})
	}()
	d, _ := OutputCollector{}.Collect(context.Background(), bus, "w1", 80, 5000)
	if d != "real" {
		t.Fatalf("delta = %q, want real", d)
	}
}

func TestCollectorWorkerDisconnectSentinel(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	go func() {
		if !waitForSub(bus, "w1") {
			return
		}
		bus.Enqueue("w1", map[string]any{"type": "term", "data": map[string]any{"data": "partial"}})
		bus.CloseWorker("w1") // nil sentinel → collector returns immediately
	}()
	d, _ := OutputCollector{}.Collect(context.Background(), bus, "w1", 10000, 10000)
	if d != "partial" {
		t.Fatalf("delta = %q, want partial (sentinel break)", d)
	}
}

func TestCollectorContextCancel(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		if !waitForSub(bus, "w1") {
			return
		}
		cancel()
	}()
	// Large quiesce + max so only ctx cancellation ends the collect.
	d, _ := OutputCollector{}.Collect(ctx, bus, "w1", 10000, 10000)
	if d != "" {
		t.Fatalf("delta = %q, want empty on cancel", d)
	}
}

func TestCollectorHardCap(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	stop := make(chan struct{})
	go func() {
		if !waitForSub(bus, "w1") {
			return
		}
		for {
			select {
			case <-stop:
				return
			default:
				bus.Enqueue("w1", map[string]any{"type": "term", "data": map[string]any{"data": "x"}})
				time.Sleep(2 * time.Millisecond)
			}
		}
	}()
	// Large quiesce so only the hard cap ends collection.
	d, e := OutputCollector{}.Collect(context.Background(), bus, "w1", 10000, 120)
	close(stop)
	if len(d) == 0 {
		t.Fatal("expected some output under hard cap")
	}
	if e < 100 {
		t.Fatalf("elapsed = %d, want >= ~120 (hard cap)", e)
	}
}

func TestCollectorMaxCapTopCheck(t *testing.T) {
	// maxMS=0 makes the deadline equal to start, so the first loop iteration
	// sees remaining<=0 and exits via the top-of-loop max guard immediately.
	bus := hub.NewEventBus(hub.EventBusOptions{})
	d, e := OutputCollector{}.Collect(context.Background(), bus, "w1", 10000, 0)
	if d != "" || e < 0 {
		t.Fatalf("max=0 Collect = (%q,%d), want (\"\", >=0)", d, e)
	}
}

func TestCollectorWatchError(t *testing.T) {
	// A bus capped at one subscriber per worker: fill it, then Collect must hit
	// the Watch error branch and return ("",0).
	bus := hub.NewEventBus(hub.EventBusOptions{MaxSubscribersPerWorker: 1})
	_, remove, err := bus.Watch("w1", nil, nil)
	if err != nil {
		t.Fatalf("first watch: %v", err)
	}
	defer remove()
	d, e := OutputCollector{}.Collect(context.Background(), bus, "w1", 50, 500)
	if d != "" || e != 0 {
		t.Fatalf("watch-error Collect = (%q,%d), want (\"\",0)", d, e)
	}
}
