//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"testing"
	"time"
)

// Ported from packages/provide-uterm-server/tests/bridge/test_base*.py.

func TestHijackableNotHijackedReturnsImmediately(t *testing.T) {
	h := NewHijackable(nil)
	if h.IsHijacked() {
		t.Fatal("should not be hijacked by default")
	}
	if err := h.AwaitIfHijacked(context.Background()); err != nil {
		t.Fatalf("await while not hijacked should return nil, got %v", err)
	}
}

func TestHijackableSetHijackedIdempotent(t *testing.T) {
	h := NewHijackable(nil)
	h.SetHijacked(false) // same as default → early return, no state change
	if h.IsHijacked() {
		t.Fatal("still not hijacked")
	}
	h.SetHijacked(true)
	h.SetHijacked(true) // idempotent
	if !h.IsHijacked() {
		t.Fatal("should be hijacked")
	}
}

func TestHijackableBlocksThenResumes(t *testing.T) {
	h := NewHijackable(nil)
	h.SetHijacked(true)
	done := make(chan error, 1)
	go func() { done <- h.AwaitIfHijacked(context.Background()) }()

	select {
	case <-done:
		t.Fatal("await should block while hijacked")
	case <-time.After(50 * time.Millisecond):
	}

	h.SetHijacked(false)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("await should return nil after resume, got %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("await did not return after resume")
	}
}

func TestHijackableAwaitContextCancel(t *testing.T) {
	h := NewHijackable(nil)
	h.SetHijacked(true)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.AwaitIfHijacked(ctx) }()
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("await should return ctx error on cancel")
		}
	case <-time.After(time.Second):
		t.Fatal("await did not observe cancellation")
	}
}

func TestHijackableStepTokens(t *testing.T) {
	h := NewHijackable(nil)
	// No-op when not hijacked.
	h.RequestStep(2)
	if h.StepTokens() != 0 {
		t.Fatal("request_step is a no-op when not hijacked")
	}
	h.SetHijacked(true)
	h.RequestStep(2)
	if h.StepTokens() != 2 {
		t.Fatalf("expected 2 tokens, got %d", h.StepTokens())
	}
	// Two checkpoint passes consume both tokens without blocking.
	if err := h.AwaitIfHijacked(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := h.AwaitIfHijacked(context.Background()); err != nil {
		t.Fatal(err)
	}
	if h.StepTokens() != 0 {
		t.Fatalf("expected 0 tokens after two passes, got %d", h.StepTokens())
	}
	// A third pass blocks again.
	blocked := make(chan error, 1)
	go func() { blocked <- h.AwaitIfHijacked(context.Background()) }()
	select {
	case <-blocked:
		t.Fatal("third pass should block")
	case <-time.After(50 * time.Millisecond):
	}
	h.SetHijacked(false)
	<-blocked
}

func TestHijackableRequestStepCapsAndFloors(t *testing.T) {
	h := NewHijackable(nil)
	h.SetHijacked(true)
	h.RequestStep(-5) // floored to 0
	if h.StepTokens() != 0 {
		t.Fatalf("negative checkpoints floored to 0, got %d", h.StepTokens())
	}
	h.RequestStep(1000) // capped at 100
	if h.StepTokens() != 100 {
		t.Fatalf("tokens capped at 100, got %d", h.StepTokens())
	}
	h.RequestStep(50) // stays capped
	if h.StepTokens() != 100 {
		t.Fatalf("tokens stay capped at 100, got %d", h.StepTokens())
	}
}

func TestHijackableCleanupHijack(t *testing.T) {
	h := NewHijackable(nil)
	h.SetHijacked(true)
	h.CleanupHijack() // resumes + stops (no) watchdog
	if h.IsHijacked() {
		t.Fatal("cleanup should resume")
	}
	// StopWatchdog is idempotent even when none is running.
	h.StopWatchdog()
}

func TestHijackableWatchdogFires(t *testing.T) {
	h := NewHijackable(nil)
	fired := make(chan struct{}, 1)
	h.StartWatchdog(WatchdogOptions{
		StuckTimeout:  time.Millisecond,
		CheckInterval: time.Millisecond, // floored to 500ms
		OnStuck:       func() { fired <- struct{}{} },
	})
	// Second call is a no-op while one is running.
	h.StartWatchdog(WatchdogOptions{StuckTimeout: time.Second, OnStuck: func() {}})
	select {
	case <-fired:
	case <-time.After(3 * time.Second):
		t.Fatal("watchdog did not fire")
	}
	h.StopWatchdog()
	h.StopWatchdog() // idempotent
}

func TestHijackableWatchdogSuppressedWhileHijacked(t *testing.T) {
	h := NewHijackable(nil)
	h.SetHijacked(true)
	fired := make(chan struct{}, 1)
	h.StartWatchdog(WatchdogOptions{
		StuckTimeout:  time.Millisecond,
		CheckInterval: time.Millisecond, // floored to 500ms
		OnStuck:       func() { fired <- struct{}{} },
	})
	// One tick passes; while hijacked the watchdog resets progress instead of
	// firing.
	select {
	case <-fired:
		t.Fatal("watchdog must not fire while hijacked")
	case <-time.After(700 * time.Millisecond):
	}
	h.StopWatchdog()
}

func TestHijackableWatchdogNotStuckYet(t *testing.T) {
	h := NewHijackable(nil)
	fired := make(chan struct{}, 1)
	// StuckTimeout longer than the tick interval → the poll sees idle < timeout
	// and continues without firing.
	h.StartWatchdog(WatchdogOptions{
		StuckTimeout:  10 * time.Second,
		CheckInterval: time.Millisecond, // floored to 500ms
		OnStuck:       func() { fired <- struct{}{} },
	})
	select {
	case <-fired:
		t.Fatal("watchdog should not fire before the stuck timeout")
	case <-time.After(700 * time.Millisecond):
	}
	h.StopWatchdog()
}

func TestHijackableWatchdogDefaults(t *testing.T) {
	h := NewHijackable(nil)
	// Zero options select the 120s / 5s defaults; start then stop to cover the
	// default-assignment branches without waiting for a fire.
	h.StartWatchdog(WatchdogOptions{})
	h.StopWatchdog()
}

func TestHijackableWatchdogCallbackPanicRecovered(t *testing.T) {
	h := NewHijackable(nil)
	fired := make(chan struct{}, 1)
	h.StartWatchdog(WatchdogOptions{
		StuckTimeout:  time.Millisecond,
		CheckInterval: time.Millisecond,
		OnStuck: func() {
			select {
			case fired <- struct{}{}:
			default:
			}
			panic("boom")
		},
	})
	select {
	case <-fired:
	case <-time.After(3 * time.Second):
		t.Fatal("watchdog did not fire")
	}
	// A panic in the callback must not crash the watchdog goroutine.
	h.StopWatchdog()
}

func TestHijackableNoteProgress(t *testing.T) {
	h := NewHijackable(nil)
	h.NoteProgress() // just exercises the setter
}
