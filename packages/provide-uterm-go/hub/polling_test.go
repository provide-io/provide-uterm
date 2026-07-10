//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"math"
	"strings"
	"sync"
	"testing"
)

type pollFixture struct {
	p        *PollingCoordinator
	clock    *ManualClock
	requests *[]string
}

func makePolling(states map[string]map[string]any) pollFixture {
	reg := NewWorkerRegistry()
	for id, snap := range states {
		st := NewWorkerTermState()
		st.LastSnapshot = snap
		reg.Put(id, st)
	}
	clk := NewManualClock(5000) // wall 5000, mono 0
	var requests []string
	p := NewPollingCoordinator(reg, &sync.Mutex{}, clk, func(_ context.Context, wid string) error {
		requests = append(requests, wid)
		return nil
	})
	return pollFixture{p: p, clock: clk, requests: &requests}
}

// makePollingWorkerNil registers a present worker whose snapshot is nil.
func makePollingWorkerNil() pollFixture {
	reg := NewWorkerRegistry()
	reg.Put("w", NewWorkerTermState())
	clk := NewManualClock(5000)
	var requests []string
	p := NewPollingCoordinator(reg, &sync.Mutex{}, clk, func(_ context.Context, wid string) error {
		requests = append(requests, wid)
		return nil
	})
	return pollFixture{p: p, clock: clk, requests: &requests}
}

func TestSnapshotMatchesNilFalse(t *testing.T) {
	mustFalse(t, SnapshotMatches(nil, "", nil), "nil snapshot")
}

func TestCompileGuardRegex(t *testing.T) {
	re, err := compileGuardRegex("")
	mustTrue(t, re == nil && err == "", "empty -> none")

	re, err = compileGuardRegex("^ab+c")
	mustTrue(t, err == "" && re != nil, "valid compiles")
	mustTrue(t, re.MatchString("XX\nABBBC"), "ignorecase + multiline applied")

	re, err = compileGuardRegex("(unclosed")
	mustTrue(t, re == nil && err != "", "invalid errors")
	mustTrue(t, contains2(err, "invalid expect_regex"), "invalid message")
}

func TestWaitForSnapshotReturnsFresh(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "fresh", "ts": 5001.0}})
	out, err := f.p.WaitForSnapshot(context.Background(), "w", 1500)
	mustTrue(t, err == nil && out != nil, "fresh returned")
	mustDeepEqual(t, *f.requests, []string{"w"}, "requested worker (not nil)")
}

func TestWaitForSnapshotMissingWorker(t *testing.T) {
	f := makePolling(nil)
	out, _ := f.p.WaitForSnapshot(context.Background(), "ghost", 1500)
	mustTrue(t, out == nil, "missing worker -> nil")
}

func TestWaitForSnapshotDeadlineArithmetic(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "stale", "ts": 1.0}})
	out, _ := f.p.WaitForSnapshot(context.Background(), "w", 1001000)
	mustTrue(t, out == nil, "never fresh -> nil")
	mustEqual(t, len(f.clock.Sleeps()), 1001, "timeout/1000 polls")
	mustEqual(t, f.clock.Sleeps()[0], 0.08, "sleep constant 0.08")
}

func TestWaitForSnapshotMissingTSDefaultsZero(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "no-ts"}})
	out, _ := f.p.WaitForSnapshot(context.Background(), "w", 2000)
	mustTrue(t, out == nil, "no-ts never fresh")
	mustEqual(t, len(f.clock.Sleeps()), 2, "2 polls")
}

func TestWaitForSnapshotMissingTSBelowReqTS(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "no-ts"}})
	f.clock.SetWall(0.5)
	out, _ := f.p.WaitForSnapshot(context.Background(), "w", 2000)
	mustTrue(t, out == nil, "default 0 not > 0.5")
}

func TestWaitForSnapshotTSEqualReqNotFresh(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "boundary", "ts": 1234.0}})
	f.clock.SetWall(1234.0)
	out, _ := f.p.WaitForSnapshot(context.Background(), "w", 2000)
	mustTrue(t, out == nil, "ts == req not fresh (strict >)")
}

func TestWaitForGuardRegexErrorShortCircuits(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "x"}})
	ok, out, reason, _ := f.p.WaitForGuard(context.Background(), "w", "", "(unclosed", 1000, 20)
	mustFalse(t, ok, "not ok")
	mustTrue(t, out == nil, "no snapshot")
	mustTrue(t, contains2(reason, "invalid expect_regex"), "reason")
	mustEqual(t, len(*f.requests), 0, "never requested")
}

func TestWaitForGuardFastPath(t *testing.T) {
	snap := map[string]any{"screen": "ready", "ts": 1.0}
	f := makePolling(map[string]map[string]any{"w": snap})
	ok, out, reason, _ := f.p.WaitForGuard(context.Background(), "w", "", "", 1000, 20)
	mustTrue(t, ok, "ok")
	mustTrue(t, reason == "" && sameMap(out, snap), "returns existing snapshot")
	mustDeepEqual(t, *f.requests, []string{"w"}, "one request for worker")
}

func TestWaitForGuardFastPathMissingWorker(t *testing.T) {
	f := makePolling(nil)
	ok, out, reason, _ := f.p.WaitForGuard(context.Background(), "ghost", "", "", 1000, 20)
	mustTrue(t, ok && out == nil && reason == "", "no snapshot, still ok")
}

func TestWaitForGuardMatchesImmediately(t *testing.T) {
	snap := map[string]any{"prompt_detected": map[string]any{"prompt_id": "menu"}, "screen": "Menu"}
	f := makePolling(map[string]map[string]any{"w": snap})
	ok, _, reason, _ := f.p.WaitForGuard(context.Background(), "w", "menu", "", 1000, 20)
	mustTrue(t, ok && reason == "", "matched")
	mustDeepEqual(t, *f.requests, []string{"w"}, "matched before re-request")
}

func TestWaitForGuardDeadlineAndInterval(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "no-prompt", "ts": 7.0}})
	ok, _, reason, _ := f.p.WaitForGuard(context.Background(), "w", "never", "", 1001000, 20)
	mustFalse(t, ok, "never matched")
	mustEqual(t, reason, "prompt_guard_not_satisfied", "reason")
	mustEqual(t, len(f.clock.Sleeps()), 1001, "timeout/1000 polls")
	mustTrue(t, math.Abs(f.clock.Sleeps()[0]-0.02) < 1e-9, "interval max(20,20)/1000")
}

func TestWaitForGuardSmallTimeout(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "no-prompt", "ts": 7.0}})
	ok, out, reason, _ := f.p.WaitForGuard(context.Background(), "w", "never", "", 50, 20)
	mustFalse(t, ok, "not matched")
	mustEqual(t, reason, "prompt_guard_not_satisfied", "reason")
	mustDeepEqual(t, out, map[string]any{"screen": "no-prompt", "ts": 7.0}, "last snapshot returned")
	mustEqual(t, len(f.clock.Sleeps()), 1, "one poll (max(50,50)/1000)")
	mustTrue(t, math.Abs(f.clock.Sleeps()[0]-0.02) < 1e-9, "interval")
}

func TestWaitForGuardRegexPassedToMatcher(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "totally other"}})
	ok, _, reason, _ := f.p.WaitForGuard(context.Background(), "w", "", "WONTMATCH", 5000, 20)
	mustFalse(t, ok, "non-matching regex keeps polling")
	mustEqual(t, reason, "prompt_guard_not_satisfied", "reason")
}

func TestWaitForGuardAllRequestsTargetWorker(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "x", "ts": 3.0}})
	_, _, _, _ = f.p.WaitForGuard(context.Background(), "w", "never", "", 5000, 20)
	mustTrue(t, len(*f.requests) > 0, "at least one request")
	for _, r := range *f.requests {
		mustEqual(t, r, "w", "targets worker")
	}
}

func TestWaitForGuardRerequestStaticTS(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "x", "ts": 0.5}})
	_, _, _, _ = f.p.WaitForGuard(context.Background(), "w", "never", "", 5000, 20)
	mustEqual(t, len(f.clock.Sleeps()), 5, "5 polls")
	mustEqual(t, len(*f.requests), 5, "pre + 4 re-requests")
}

func TestWaitForGuardMissingTSRerequestsEveryPoll(t *testing.T) {
	f := makePolling(map[string]map[string]any{"w": {"screen": "no-ts"}})
	_, _, _, _ = f.p.WaitForGuard(context.Background(), "w", "never", "", 5000, 20)
	mustEqual(t, len(f.clock.Sleeps()), 5, "5 polls")
	mustEqual(t, len(*f.requests), 6, "pre + re-request every poll")
}

func TestWaitForGuardNilSnapshotElseBranch(t *testing.T) {
	f := makePollingWorkerNil()
	_, _, _, _ = f.p.WaitForGuard(context.Background(), "w", "never", "", 5000, 20)
	mustEqual(t, len(f.clock.Sleeps()), 5, "5 polls")
	mustEqual(t, len(*f.requests), 6, "pre + re-request every poll (else default 0)")
}

// errSleepClock is a ManualClock whose Sleep returns a fixed error.
type errSleepClock struct {
	*ManualClock
	err error
}

func (c *errSleepClock) Sleep(context.Context, float64) error { return c.err }

func newRegistryWithSnap(snap map[string]any) *WorkerRegistry {
	reg := NewWorkerRegistry()
	st := NewWorkerTermState()
	st.LastSnapshot = snap
	reg.Put("w", st)
	return reg
}

func TestWaitForSnapshotRequestError(t *testing.T) {
	reg := newRegistryWithSnap(map[string]any{"screen": "x", "ts": 1.0})
	boom := errString("req boom")
	p := NewPollingCoordinator(reg, &sync.Mutex{}, NewManualClock(5000), func(context.Context, string) error {
		return boom
	})
	_, err := p.WaitForSnapshot(context.Background(), "w", 1000)
	mustTrue(t, err == error(boom), "request error propagates")
}

func TestWaitForSnapshotSleepError(t *testing.T) {
	reg := newRegistryWithSnap(map[string]any{"screen": "stale", "ts": 1.0})
	clk := &errSleepClock{ManualClock: NewManualClock(5000), err: errString("sleep boom")}
	p := NewPollingCoordinator(reg, &sync.Mutex{}, clk, func(context.Context, string) error { return nil })
	_, err := p.WaitForSnapshot(context.Background(), "w", 1000)
	mustTrue(t, err != nil, "sleep error propagates")
}

func TestWaitForGuardRequestErrors(t *testing.T) {
	boom := errString("req boom")
	// Fast path request error.
	reg := newRegistryWithSnap(map[string]any{"screen": "x"})
	p := NewPollingCoordinator(reg, &sync.Mutex{}, NewManualClock(5000), func(context.Context, string) error { return boom })
	_, _, _, err := p.WaitForGuard(context.Background(), "w", "", "", 1000, 20)
	mustTrue(t, err == error(boom), "fast-path request error")

	// Pre-loop request error (guard active).
	reg2 := newRegistryWithSnap(map[string]any{"screen": "x"})
	p2 := NewPollingCoordinator(reg2, &sync.Mutex{}, NewManualClock(5000), func(context.Context, string) error { return boom })
	_, _, _, err = p2.WaitForGuard(context.Background(), "w", "never", "", 1000, 20)
	mustTrue(t, err == error(boom), "pre-loop request error")

	// In-loop re-request error (errors on the 2nd call).
	reg3 := newRegistryWithSnap(map[string]any{"screen": "x", "ts": 1.0})
	calls := 0
	p3 := NewPollingCoordinator(reg3, &sync.Mutex{}, NewManualClock(5000), func(context.Context, string) error {
		calls++
		if calls >= 2 {
			return boom
		}
		return nil
	})
	_, _, _, err = p3.WaitForGuard(context.Background(), "w", "never", "", 5000, 20)
	mustTrue(t, err == error(boom), "in-loop re-request error")
}

func TestWaitForGuardSleepError(t *testing.T) {
	reg := newRegistryWithSnap(map[string]any{"screen": "no-prompt", "ts": 7.0})
	clk := &errSleepClock{ManualClock: NewManualClock(5000), err: errString("sleep boom")}
	p := NewPollingCoordinator(reg, &sync.Mutex{}, clk, func(context.Context, string) error { return nil })
	_, _, _, err := p.WaitForGuard(context.Background(), "w", "never", "", 5000, 20)
	mustTrue(t, err != nil, "guard sleep error propagates")
}

func TestWaitForGuardWorkerVanishesMidLoop(t *testing.T) {
	// The worker is removed during the pre-loop request, so the loop reads a nil
	// state (the else branch that nils lastSnapshot).
	reg := newRegistryWithSnap(map[string]any{"screen": "x", "ts": 1.0})
	clk := NewManualClock(5000)
	popped := false
	p := NewPollingCoordinator(reg, &sync.Mutex{}, clk, func(context.Context, string) error {
		if !popped {
			popped = true
			reg.Pop("w")
		}
		return nil
	})
	ok, out, reason, _ := p.WaitForGuard(context.Background(), "w", "never", "", 3000, 20)
	mustFalse(t, ok, "never matched (worker gone)")
	mustTrue(t, out == nil, "nil snapshot after worker vanished")
	mustEqual(t, reason, "prompt_guard_not_satisfied", "reason")
}

func TestMaxInt(t *testing.T) {
	mustEqual(t, maxInt(50, 10), 50, "a > b")
	mustEqual(t, maxInt(10, 50), 50, "a < b")
	mustEqual(t, maxInt(7, 7), 7, "equal")
}

func contains2(s, substr string) bool { return strings.Contains(s, substr) }

func sameMap(a, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}
