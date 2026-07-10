//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"sync"
	"testing"
)

// storeFixture bundles a store with its registry + clock.
type storeFixture struct {
	store    *StateStore
	registry *WorkerRegistry
	clock    *ManualClock
	logbuf   func() string
}

func makeStore(cfg StateStoreConfig) storeFixture {
	reg := cfg.Registry
	if reg == nil {
		reg = NewWorkerRegistry()
	}
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	logger, buf := captureLogger()
	cfg.Registry = reg
	cfg.Lock = &sync.Mutex{}
	cfg.Clock = clk
	cfg.Logger = logger
	if cfg.MaxBufferChars == 0 {
		cfg.MaxBufferChars = 40000
	}
	return storeFixture{
		store:    NewStateStore(cfg),
		registry: reg,
		clock:    clk,
		logbuf:   buf.String,
	}
}

func TestBufferPartialInputAccumulates(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	ws := newBrowser("ws")
	cmd, ok := f.store.BufferAndGetCommand(ws, "ls ")
	mustFalse(t, ok, "no command yet")
	mustEqual(t, cmd, "", "empty")
	mustEqual(t, f.store.inputBuffers[ws], "ls ", "buffered")
}

func TestBufferConcatenatesPriorSegment(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	ws := newBrowser("ws")
	f.store.BufferAndGetCommand(ws, "ab")
	cmd, ok := f.store.BufferAndGetCommand(ws, "cd\n")
	mustTrue(t, ok, "command")
	mustEqual(t, cmd, "abcd\n", "concatenated")
	_, present := f.store.inputBuffers[ws]
	mustFalse(t, present, "popped on completion")
}

func TestBufferNewlineAndCR(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	cmd, ok := f.store.BufferAndGetCommand(newBrowser("a"), "help\n")
	mustTrue(t, ok && cmd == "help\n", "newline flushes")
	cmd, ok = f.store.BufferAndGetCommand(newBrowser("b"), "help\r")
	mustTrue(t, ok && cmd == "help\r", "CR flushes")
}

func TestBufferAtLimitKeptOverLimitDropped(t *testing.T) {
	f := makeStore(StateStoreConfig{MaxBufferChars: 4})
	ws := newBrowser("ws")
	_, ok := f.store.BufferAndGetCommand(ws, "abcd") // len==max kept
	mustFalse(t, ok, "no command")
	mustEqual(t, f.store.inputBuffers[ws], "abcd", "kept at limit")
	_, ok = f.store.BufferAndGetCommand(ws, "e") // len 5 > 4 discarded
	mustFalse(t, ok, "over limit dropped")
	_, present := f.store.inputBuffers[ws]
	mustFalse(t, present, "buffer cleared")
}

func TestBufferOverLimitFirstWrite(t *testing.T) {
	f := makeStore(StateStoreConfig{MaxBufferChars: 4})
	ws := newBrowser("ws")
	_, ok := f.store.BufferAndGetCommand(ws, "abcdef") // 6>4, ws never buffered
	mustFalse(t, ok, "discarded")
	_, present := f.store.inputBuffers[ws]
	mustFalse(t, present, "not buffered")
}

func TestTouchActivity(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	st := NewWorkerTermState()
	st.LastActivityAt = 1.0
	f.registry.Put("w", st)
	f.store.TouchActivity("w")
	mustEqual(t, st.LastActivityAt, 1000.0, "monotonic stamp written")

	f.store.TouchActivity("ghost") // must not panic
}

func TestGetOrCreate(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	created := f.store.GetOrCreate("w")
	mustTrue(t, f.registry.Get("w") == created, "registered")
	again := f.store.GetOrCreate("w")
	mustTrue(t, again == created, "existing returned")
}

func TestMetricNoCallback(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	f.store.Metric("x", 1) // no callback, must not panic
}

func TestMetricInvokesCallback(t *testing.T) {
	var seen [][2]any
	f := makeStore(StateStoreConfig{OnMetric: func(name string, value int) {
		seen = append(seen, [2]any{name, value})
	}})
	f.store.Metric("hits", 2)
	mustDeepEqual(t, seen, [][2]any{{"hits", 2}}, "name+value passed")
}

func TestMetricSwallowsPanic(t *testing.T) {
	f := makeStore(StateStoreConfig{OnMetric: func(string, int) { panic("boom") }})
	f.store.Metric("hits", 3) // must not panic
	mustTrue(t, logContains(f.logbuf(), "metric_callback_failed"), "logged failure")
}

func TestClampLease(t *testing.T) {
	cases := []struct{ in, want int }{{0, 1}, {-10, 1}, {50, 50}, {14400, 14400}, {20000, 14400}}
	for _, c := range cases {
		mustEqual(t, ClampLease(c.in), c.want, "clamp")
	}
}

func TestHasValidRESTLease(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	mustFalse(t, f.store.HasValidRESTLease(NewWorkerTermState()), "no session")

	future := NewWorkerTermState()
	future.HijackSession = restSession("h", "o", 1100)
	mustTrue(t, f.store.HasValidRESTLease(future), "future valid")

	past := NewWorkerTermState()
	past.HijackSession = restSession("h", "o", 900)
	mustFalse(t, f.store.HasValidRESTLease(past), "past invalid")

	boundary := NewWorkerTermState()
	boundary.HijackSession = restSession("h", "o", 1000) // == now
	mustFalse(t, f.store.HasValidRESTLease(boundary), "== now invalid")
}

func TestIsDashboardHijackActive(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	mustFalse(t, f.store.IsDashboardHijackActive(NewWorkerTermState()), "no owner")

	perpetual := NewWorkerTermState()
	perpetual.HijackOwner = newBrowser("o")
	mustTrue(t, f.store.IsDashboardHijackActive(perpetual), "owner no expiry -> perpetual")

	future := NewWorkerTermState()
	future.HijackOwner = newBrowser("o")
	future.HijackOwnerExpiresAt = f64p(1100)
	mustTrue(t, f.store.IsDashboardHijackActive(future), "future")

	past := NewWorkerTermState()
	past.HijackOwner = newBrowser("o")
	past.HijackOwnerExpiresAt = f64p(900)
	mustFalse(t, f.store.IsDashboardHijackActive(past), "past")

	boundary := NewWorkerTermState()
	boundary.HijackOwner = newBrowser("o")
	boundary.HijackOwnerExpiresAt = f64p(1000)
	mustFalse(t, f.store.IsDashboardHijackActive(boundary), "== now inactive")
}

func TestIsHijacked(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	dash := NewWorkerTermState()
	dash.HijackOwner = newBrowser("o")
	mustTrue(t, f.store.IsHijacked(dash), "dashboard")

	rest := NewWorkerTermState()
	rest.HijackSession = restSession("h", "o", 1100)
	mustTrue(t, f.store.IsHijacked(rest), "rest")

	mustFalse(t, f.store.IsHijacked(NewWorkerTermState()), "neither")
}

func TestNotifyHijackChanged(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	f.store.NotifyHijackChanged("w", true, strp("me")) // no callback, noop

	var seen []notifyCall
	f2 := makeStore(StateStoreConfig{OnHijackChanged: func(wid string, enabled bool, owner *string) error {
		seen = append(seen, notifyCall{wid, enabled, owner})
		return nil
	}})
	f2.store.NotifyHijackChanged("w", true, strp("me"))
	mustEqual(t, len(seen), 1, "callback invoked")
	mustEqual(t, seen[0].workerID, "w", "worker id")
	mustTrue(t, seen[0].enabled, "enabled")
}

func TestNotifyHijackChangedErrorLogged(t *testing.T) {
	f := makeStore(StateStoreConfig{OnHijackChanged: func(string, bool, *string) error {
		return context.Canceled
	}})
	f.store.NotifyHijackChanged("w", true, strp("me"))
	mustTrue(t, logContains(f.logbuf(), "on_hijack_changed callback raised"), "error logged")
}

func TestShutdown(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	mustEqual(t, f.store.Shutdown(), 0, "no tasks -> 0")

	ctx, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	go func() {
		<-ctx.Done()
		result <- ctx.Err()
	}()
	f.store.Tasks().Add(cancel, result)
	mustEqual(t, f.store.Shutdown(), 1, "one task cancelled")
	mustTrue(t, logContains(f.logbuf(), "hub_shutdown"), "logged shutdown")
}

func TestShutdownNormalCompletionNotCounted(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	_, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	result <- nil // task already completed normally
	f.store.Tasks().Add(cancel, result)
	mustEqual(t, f.store.Shutdown(), 0, "normal completion not counted")
}
