//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"fmt"
	"log/slog"
	"regexp"
	"strconv"
	"sync"
	"sync/atomic"
)

// Subscription is a single active event-bus watcher. Port of the Python
// _Subscription dataclass. Queue carries delivered events; a nil map is the
// worker-disconnected sentinel.
type Subscription struct {
	ID         string
	WorkerID   string
	Queue      chan map[string]any
	eventTypes map[string]bool // nil = accept all types
	pattern    *regexp.Regexp  // nil = no text filter

	// Atomic because deliver runs OUTSIDE the bus lock -- Enqueue copies the
	// target list under b.mu and releases it before delivering, so two
	// producers for one worker increment this concurrently. A plain int is a
	// data race there, which -race reports and which can lose counts.
	dropped atomic.Int64

	// The bus this subscription came from, for PendingOverride.
	bus *EventBus
}

// Dropped returns the number of events dropped for this subscription.
func (s *Subscription) Dropped() int { return int(s.dropped.Load()) }

// Pending reports how many events are buffered here and not yet consumed.
//
// Reads through the bus so a harness can state that a producer never falls
// quiet, rather than racing one -- see [EventBus.PendingOverride].
func (s *Subscription) Pending() int {
	if s.bus != nil && s.bus.PendingOverride != nil {
		if n, ok := s.bus.PendingOverride(s.WorkerID); ok {
			return n
		}
	}
	return len(s.Queue)
}

// EventBus is the real-time event fanout layer. Port of
// provide.uterm.server.bridge.hub.event_bus.EventBus.
//
// Subscribers open a watch; the hub calls Enqueue (outside the hub lock) to
// deliver events via non-blocking channel sends. On worker disconnect the hub
// calls CloseWorker to push a nil sentinel and release all that worker's
// subscribers.
//
// Deviation: the Python bus relies on the single event loop; this port guards
// the subscription registry with a mutex and delivers via buffered channels so
// it is safe under -race.
type EventBus struct {
	maxQueueDepth           int
	maxSubscribersPerWorker int
	maxPatternLength        int
	maxMatchInputChars      int
	onMetric                func(name string, value int)
	logger                  *slog.Logger

	// PendingOverride, when set, answers [Subscription.Pending] for the workers
	// it recognises; nil in production.
	//
	// It exists for the differential fan-out scenarios. They assert that a
	// member is reported cut short when output is STILL QUEUED as the collect
	// exits, and proving that by out-producing the collector is a scheduling
	// race rather than a test: the harness measured 3 of 40 collects draining
	// dry with one producer, and after being hardened to four it still reached
	// CI as `total_response_deadline.failed_members=[] want=["w1"]`. Stating
	// the depth directly tests the derivation itself, which is the actual
	// contract. TypeScript and C# already express it this way in their
	// harnesses; this is the seam Go lacked.
	PendingOverride func(workerID string) (int, bool)

	mu     sync.Mutex
	subs   map[string][]*Subscription
	nextID atomic.Uint64
}

// EventBusOptions configures an [EventBus]. Zero values select the Python
// defaults (queue depth 500, 100 subscribers/worker, pattern length 512, match
// input 8192).
type EventBusOptions struct {
	MaxQueueDepth           int
	MaxSubscribersPerWorker int
	MaxPatternLength        int
	MaxMatchInputChars      int
	OnMetric                func(name string, value int)
	Logger                  *slog.Logger
}

// NewEventBus builds an event bus with the given options.
func NewEventBus(opts EventBusOptions) *EventBus {
	pick := func(v, def int) int {
		if v < 1 {
			return def
		}
		return v
	}
	return &EventBus{
		maxQueueDepth:           pick(opts.MaxQueueDepth, 500),
		maxSubscribersPerWorker: pick(opts.MaxSubscribersPerWorker, 100),
		maxPatternLength:        pick(opts.MaxPatternLength, defaultMaxPatternLength),
		maxMatchInputChars:      pick(opts.MaxMatchInputChars, defaultMaxMatchInputChars),
		onMetric:                opts.OnMetric,
		logger:                  loggerOrDefault(opts.Logger),
		subs:                    map[string][]*Subscription{},
	}
}

// Enqueue delivers event to all subscribers for workerID. Port of _enqueue:
// takes a snapshot of the targets under lock then delivers lock-free. A panic
// in delivery is recovered and logged so it never propagates to the caller.
func (b *EventBus) Enqueue(workerID string, event map[string]any) {
	defer func() {
		if r := recover(); r != nil {
			b.logger.Warn("event_bus_enqueue_error", "worker_id", workerID, "error", r)
		}
	}()
	b.mu.Lock()
	targets := append([]*Subscription(nil), b.subs[workerID]...)
	b.mu.Unlock()
	for _, sub := range targets {
		b.deliver(sub, workerID, event)
	}
}

// deliver filters and enqueues event to a single subscription with ring-buffer
// (drop-oldest) overflow semantics. Port of _deliver.
func (b *EventBus) deliver(sub *Subscription, workerID string, event map[string]any) {
	if sub.eventTypes != nil {
		t, _ := event["type"].(string)
		if !sub.eventTypes[t] {
			return
		}
	}
	if sub.pattern != nil {
		screen := extractScreen(event)
		if len(screen) > b.maxMatchInputChars {
			screen = screen[:b.maxMatchInputChars]
		}
		if !sub.pattern.MatchString(screen) {
			return
		}
	}
	item := map[string]any{"worker_id": workerID}
	for k, v := range event {
		item[k] = v
	}
	select {
	case sub.Queue <- item:
	default:
		// Ring-buffer: drop oldest, enqueue new.
		select {
		case <-sub.Queue:
		default:
		}
		sub.dropped.Add(1)
		if b.onMetric != nil {
			b.onMetric("event_bus_subscriber_drop_total", 1)
		}
		select {
		case sub.Queue <- item:
		default:
		}
	}
}

// extractScreen pulls event["data"]["screen"] as a string, defaulting to "".
func extractScreen(event map[string]any) string {
	data, ok := event["data"].(map[string]any)
	if !ok {
		return ""
	}
	sv, ok := data["screen"]
	if !ok {
		return ""
	}
	if s, ok := sv.(string); ok {
		return s
	}
	return fmt.Sprint(sv)
}

// CloseWorker signals end-of-stream to all subscribers for workerID by pushing
// a nil sentinel and removing the worker's subscription list. Port of
// close_worker.
func (b *EventBus) CloseWorker(workerID string) {
	b.mu.Lock()
	subs := b.subs[workerID]
	delete(b.subs, workerID)
	b.mu.Unlock()
	for _, sub := range subs {
		b.putSentinel(sub)
	}
}

// putSentinel puts a nil sentinel into sub's queue, dropping the oldest event
// to make room if the queue is full so the sentinel is always delivered. Port
// of _put_sentinel.
//
// Deviation: the Python version drops one item then, only as a last resort,
// clears the queue entirely (a defensive guard against a concurrent refill).
// This port instead retries send-then-drop in a loop, which delivers the
// sentinel with the same single drop in the (single-producer) common case and
// avoids an unreachable clear-everything branch.
func (b *EventBus) putSentinel(sub *Subscription) {
	for {
		select {
		case sub.Queue <- nil:
			return
		default:
		}
		// Full: drop the oldest event, account for it, and retry the send.
		select {
		case <-sub.Queue:
			sub.dropped.Add(1)
			if b.onMetric != nil {
				b.onMetric("event_bus_subscriber_drop_total", 1)
			}
		default:
		}
	}
}

// Watch registers a subscription for workerID and returns it plus a remove
// function that unsubscribes (idempotent). eventTypes nil accepts all types;
// pattern nil skips text filtering. Port of the watch context manager.
func (b *EventBus) Watch(workerID string, eventTypes []string, pattern *string) (*Subscription, func(), error) {
	b.mu.Lock()
	if len(b.subs[workerID]) >= b.maxSubscribersPerWorker {
		b.mu.Unlock()
		return nil, nil, fmt.Errorf(
			"EventBus: max subscribers (%d) reached for worker %q",
			b.maxSubscribersPerWorker, workerID,
		)
	}
	b.mu.Unlock()

	compiled, err := compilePattern2(pattern, b.maxPatternLength)
	if err != nil {
		return nil, nil, err
	}
	var typeSet map[string]bool
	if eventTypes != nil {
		typeSet = map[string]bool{}
		for _, t := range eventTypes {
			typeSet[t] = true
		}
	}
	sub := &Subscription{
		ID:         strconv.FormatUint(b.nextID.Add(1), 16),
		WorkerID:   workerID,
		Queue:      make(chan map[string]any, b.maxQueueDepth),
		eventTypes: typeSet,
		pattern:    compiled,
		bus:        b,
	}
	b.mu.Lock()
	b.subs[workerID] = append(b.subs[workerID], sub)
	b.mu.Unlock()

	var once sync.Once
	remove := func() { once.Do(func() { b.remove(sub) }) }
	return sub, remove, nil
}

// compilePattern2 returns nil for a nil pattern (no filter), else compiles.
// Mirrors event_bus._compile_pattern's None short-circuit.
func compilePattern2(pattern *string, maxPatternLength int) (*regexp.Regexp, error) {
	if pattern == nil {
		return nil, nil
	}
	return compilePattern(*pattern, maxPatternLength)
}

// remove unregisters sub from the registry (idempotent). Port of _remove.
func (b *EventBus) remove(sub *Subscription) {
	b.mu.Lock()
	defer b.mu.Unlock()
	workerSubs, ok := b.subs[sub.WorkerID]
	if !ok {
		return
	}
	remaining := workerSubs[:0:0]
	for _, s := range workerSubs {
		if s.ID != sub.ID {
			remaining = append(remaining, s)
		}
	}
	if len(remaining) > 0 {
		b.subs[sub.WorkerID] = remaining
	} else {
		delete(b.subs, sub.WorkerID)
	}
}

// SubscriberCount returns the number of active subscriptions for workerID.
func (b *EventBus) SubscriberCount(workerID string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.subs[workerID])
}
