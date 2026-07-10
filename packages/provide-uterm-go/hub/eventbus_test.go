//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

func evt(seq int, typ string, data map[string]any) map[string]any {
	return map[string]any{"seq": seq, "ts": 1.0, "type": typ, "data": data}
}

func mustWatch(t *testing.T, b *EventBus, workerID string, eventTypes []string, pattern *string) (*Subscription, func()) {
	t.Helper()
	sub, remove, err := b.Watch(workerID, eventTypes, pattern)
	if err != nil {
		t.Fatalf("watch: %v", err)
	}
	return sub, remove
}

func recv(sub *Subscription) map[string]any { return <-sub.Queue }

func TestEventBusSubscribeReceivesEnqueued(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, remove := mustWatch(t, b, "w1", nil, nil)
	defer remove()
	e := evt(1, "snapshot", map[string]any{"screen": "hello"})
	b.Enqueue("w1", e)
	item := recv(sub)
	want := map[string]any{"worker_id": "w1"}
	for k, v := range e {
		want[k] = v
	}
	mustDeepEqual(t, item, want, "item = worker_id + event")
}

func TestEventBusEnqueueUnknownWorker(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	b.Enqueue("no-such", evt(1, "x", map[string]any{})) // no-op, no panic
}

func TestEventBusEnqueueRecoversPanic(t *testing.T) {
	logger, buf := captureLogger()
	b := NewEventBus(EventBusOptions{Logger: logger})
	sub, _ := mustWatch(t, b, "w1", nil, nil)
	close(sub.Queue) // send on closed channel -> panic inside deliver
	b.Enqueue("w1", evt(1, "snapshot", map[string]any{}))
	mustTrue(t, logContains(buf.String(), "event_bus_enqueue_error"), "panic recovered + logged")
}

func TestEventBusMultipleSubscribers(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	s1, r1 := mustWatch(t, b, "w1", nil, nil)
	s2, r2 := mustWatch(t, b, "w1", nil, nil)
	defer r1()
	defer r2()
	b.Enqueue("w1", evt(1, "snapshot", map[string]any{}))
	mustEqual(t, recv(s1)["seq"].(int), 1, "s1")
	mustEqual(t, recv(s2)["seq"].(int), 1, "s2")
}

func TestEventBusEventTypesFilter(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, remove := mustWatch(t, b, "w1", []string{"snapshot"}, nil)
	defer remove()
	b.Enqueue("w1", evt(2, "input_send", map[string]any{}))
	b.Enqueue("w1", evt(1, "snapshot", map[string]any{}))
	item := recv(sub)
	mustEqual(t, item["type"].(string), "snapshot", "only snapshot")
	mustEqual(t, len(sub.Queue), 0, "input_send blocked")
}

func TestEventBusEventTypesMissingTypeBlocked(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, remove := mustWatch(t, b, "w1", []string{"snapshot"}, nil)
	defer remove()
	b.Enqueue("w1", map[string]any{"seq": 1}) // no "type" key
	mustEqual(t, len(sub.Queue), 0, "typeless blocked")
}

func TestEventBusPatternFilter(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, remove := mustWatch(t, b, "w1", nil, strp(`\$`))
	defer remove()
	b.Enqueue("w1", evt(1, "snapshot", map[string]any{"screen": "$ ls"}))
	mustEqual(t, recv(sub)["seq"].(int), 1, "matching screen")

	b.Enqueue("w1", evt(2, "snapshot", map[string]any{"screen": "hello"}))
	mustEqual(t, len(sub.Queue), 0, "non-matching blocked")

	b.Enqueue("w1", evt(3, "snapshot", map[string]any{}))
	mustEqual(t, len(sub.Queue), 0, "no screen field blocked")
}

func TestEventBusPatternNonStringScreen(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, remove := mustWatch(t, b, "w1", nil, strp(`123`))
	defer remove()
	b.Enqueue("w1", evt(1, "snapshot", map[string]any{"screen": 123})) // non-string coerced
	mustEqual(t, recv(sub)["seq"].(int), 1, "coerced screen matched")
}

func TestEventBusPatternBoundsScreen(t *testing.T) {
	b := NewEventBus(EventBusOptions{MaxMatchInputChars: 8})
	sub, remove := mustWatch(t, b, "w1", nil, strp("Z"))
	defer remove()
	b.Enqueue("w1", evt(1, "snapshot", map[string]any{"screen": "abcdefghZ"}))
	mustEqual(t, len(sub.Queue), 0, "Z beyond truncation blocked")
}

func TestEventBusQueueOverflowDropsOldest(t *testing.T) {
	b := NewEventBus(EventBusOptions{MaxQueueDepth: 2})
	sub, remove := mustWatch(t, b, "w1", nil, nil)
	defer remove()
	for i := 0; i < 4; i++ {
		b.Enqueue("w1", evt(i, "x", map[string]any{}))
	}
	mustEqual(t, len(sub.Queue), 2, "two fit")
	mustEqual(t, recv(sub)["seq"].(int), 2, "oldest two dropped")
	mustEqual(t, recv(sub)["seq"].(int), 3, "seq 3")
}

func TestEventBusQueueOverflowIncrementsDropped(t *testing.T) {
	var metrics []string
	b := NewEventBus(EventBusOptions{MaxQueueDepth: 1, OnMetric: func(name string, _ int) {
		metrics = append(metrics, name)
	}})
	sub, remove := mustWatch(t, b, "w1", nil, nil)
	defer remove()
	b.Enqueue("w1", evt(1, "x", map[string]any{}))
	b.Enqueue("w1", evt(2, "x", map[string]any{}))
	mustTrue(t, sub.Dropped() >= 1, "dropped incremented")
	mustTrue(t, contains(metrics, "event_bus_subscriber_drop_total"), "drop metric")
}

func TestEventBusCloseDeliversSentinel(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, _ := mustWatch(t, b, "w1", nil, nil)
	b.CloseWorker("w1")
	mustTrue(t, recv(sub) == nil, "sentinel is nil")
}

func TestEventBusCloseRemovesSubscriptions(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	_, remove := mustWatch(t, b, "w1", nil, nil)
	mustEqual(t, b.SubscriberCount("w1"), 1, "one sub")
	b.CloseWorker("w1")
	mustEqual(t, b.SubscriberCount("w1"), 0, "removed after close")
	remove() // idempotent after close (worker key gone)
	mustEqual(t, b.SubscriberCount("w1"), 0, "still zero")
}

func TestEventBusCloseUnknownNoop(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	b.CloseWorker("no-such") // no panic
}

func TestEventBusCloseFullQueueDeliversSentinel(t *testing.T) {
	b := NewEventBus(EventBusOptions{MaxQueueDepth: 1})
	sub, _ := mustWatch(t, b, "w1", nil, nil)
	b.Enqueue("w1", evt(1, "x", map[string]any{}))
	mustEqual(t, len(sub.Queue), 1, "full")
	b.CloseWorker("w1")
	mustTrue(t, recv(sub) == nil, "sentinel delivered (oldest dropped)")
}

func TestEventBusContextExitRemoves(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, remove := mustWatch(t, b, "w1", nil, nil)
	mustEqual(t, b.SubscriberCount("w1"), 1, "one")
	remove()
	mustEqual(t, b.SubscriberCount("w1"), 0, "removed")
	b.Enqueue("w1", evt(1, "x", map[string]any{}))
	mustEqual(t, len(sub.Queue), 0, "no delivery after removal")
}

func TestEventBusSubscriberCountTracks(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	mustEqual(t, b.SubscriberCount("w1"), 0, "start")
	_, r1 := mustWatch(t, b, "w1", nil, nil)
	mustEqual(t, b.SubscriberCount("w1"), 1, "one")
	_, r2 := mustWatch(t, b, "w1", nil, nil)
	mustEqual(t, b.SubscriberCount("w1"), 2, "two")
	r2()
	mustEqual(t, b.SubscriberCount("w1"), 1, "back to one")
	r1()
	mustEqual(t, b.SubscriberCount("w1"), 0, "zero")
}

func TestEventBusRemoveOneOfMany(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	s1, r1 := mustWatch(t, b, "w1", nil, nil)
	_, r2 := mustWatch(t, b, "w1", nil, nil)
	r2() // remove non-last -> remaining kept
	mustEqual(t, b.SubscriberCount("w1"), 1, "one remains")
	b.Enqueue("w1", evt(1, "x", map[string]any{}))
	mustEqual(t, recv(s1)["seq"].(int), 1, "survivor still receives")
	r1()
}

func TestEventBusWorkersIsolated(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	s1, r1 := mustWatch(t, b, "w1", nil, nil)
	s2, r2 := mustWatch(t, b, "w2", nil, nil)
	defer r1()
	defer r2()
	b.Enqueue("w1", evt(1, "snapshot", map[string]any{}))
	b.Enqueue("w2", evt(2, "snapshot", map[string]any{}))
	mustEqual(t, recv(s1)["worker_id"].(string), "w1", "w1 isolated")
	mustEqual(t, recv(s2)["worker_id"].(string), "w2", "w2 isolated")
}

func TestEventBusWatchMaxSubscribers(t *testing.T) {
	b := NewEventBus(EventBusOptions{MaxSubscribersPerWorker: 1})
	_, remove := mustWatch(t, b, "w1", nil, nil)
	defer remove()
	_, _, err := b.Watch("w1", nil, nil)
	mustTrue(t, err != nil, "second watch rejected at cap")
}

func TestEventBusWatchPatternTooLong(t *testing.T) {
	b := NewEventBus(EventBusOptions{MaxPatternLength: 8})
	_, _, err := b.Watch("w1", nil, strp("aaaaaaaaa")) // 9 chars
	mustTrue(t, err != nil, "over-long pattern rejected")
}

func TestEventBusPatternNoDataKeyBlocked(t *testing.T) {
	b := NewEventBus(EventBusOptions{})
	sub, remove := mustWatch(t, b, "w1", nil, strp("x"))
	defer remove()
	b.Enqueue("w1", map[string]any{"seq": 1, "type": "snapshot"}) // no "data" key
	mustEqual(t, len(sub.Queue), 0, "no data -> empty screen -> blocked")
}

func TestEventBusCloseFullQueueRecordsDropMetric(t *testing.T) {
	var metrics []string
	b := NewEventBus(EventBusOptions{MaxQueueDepth: 1, OnMetric: func(name string, _ int) {
		metrics = append(metrics, name)
	}})
	sub, _ := mustWatch(t, b, "w1", nil, nil)
	b.Enqueue("w1", evt(1, "x", map[string]any{}))
	b.CloseWorker("w1") // full -> drop oldest (metric) -> sentinel
	mustTrue(t, recv(sub) == nil, "sentinel delivered")
	mustTrue(t, contains(metrics, "event_bus_subscriber_drop_total"), "drop metric on sentinel path")
}

func TestEventBusDeliverNoMetric(t *testing.T) {
	// Overflow with nil onMetric covers the metric-nil branch in deliver.
	b := NewEventBus(EventBusOptions{MaxQueueDepth: 1})
	sub, remove := mustWatch(t, b, "w1", nil, nil)
	defer remove()
	b.Enqueue("w1", evt(1, "x", map[string]any{}))
	b.Enqueue("w1", evt(2, "x", map[string]any{}))
	mustTrue(t, sub.Dropped() >= 1, "dropped without metric callback")
}
