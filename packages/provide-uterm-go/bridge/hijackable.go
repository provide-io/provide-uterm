//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// Port of provide.uterm.bridge.base.HijackableMixin.
//
// Go has no mixin/multiple-inheritance, so the Python mixin becomes a struct a
// worker embeds (or holds) to gain checkpoint hijackability. A worker calls
// AwaitIfHijacked at checkpoints in its automation loop; a hub/manager calls
// SetHijacked to pause/resume; a dashboard calls RequestStep to release one
// (or a few) checkpoint passes while still paused.
//
// Deviation from Python: AwaitIfHijacked takes a context so a blocked
// checkpoint can be cancelled (the Python coroutine relies on task
// cancellation). The watchdog callback (OnStuck) is a plain func and any panic
// it raises is recovered and logged, mirroring the Python except-and-log.

// gate is a broadcast latch modelled on asyncio.Event. A "set" gate lets
// waiters through immediately; a "clear" gate blocks them until set. It starts
// set (matching HijackableMixin: not hijacked by default).
type gate struct {
	mu sync.Mutex
	ch chan struct{}
}

func newGate() *gate {
	ch := make(chan struct{})
	close(ch) // start set → no blocking
	return &gate{ch: ch}
}

// set releases every current and future waiter until the next clear.
func (g *gate) set() {
	g.mu.Lock()
	defer g.mu.Unlock()
	select {
	case <-g.ch: // already set (closed)
	default:
		close(g.ch)
	}
}

// clear makes waiters block until the next set.
func (g *gate) clear() {
	g.mu.Lock()
	defer g.mu.Unlock()
	select {
	case <-g.ch: // currently set (closed) → install a fresh open channel
		g.ch = make(chan struct{})
	default: // already clear
	}
}

// wait blocks until the gate is set or ctx is cancelled.
func (g *gate) wait(ctx context.Context) error {
	g.mu.Lock()
	ch := g.ch
	g.mu.Unlock()
	select {
	case <-ch:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// WatchdogOptions configure StartWatchdog. Zero values select the Python
// defaults (StuckTimeout 120s, CheckInterval 5s, no callback).
type WatchdogOptions struct {
	// StuckTimeout is the idle duration before OnStuck fires.
	StuckTimeout time.Duration
	// CheckInterval is how often the watchdog polls (floored at 500ms).
	CheckInterval time.Duration
	// OnStuck is invoked when the worker stops making progress. A panic it
	// raises is recovered and logged.
	OnStuck func()
}

// Hijackable provides pause/resume/step/watchdog primitives for a worker.
type Hijackable struct {
	mu           sync.Mutex
	hijacked     bool
	stepTokens   int
	lastProgress time.Time
	gate         *gate

	watchdogCancel context.CancelFunc
	watchdogDone   chan struct{}

	logger *slog.Logger
}

// NewHijackable builds a Hijackable. logger may be nil (a no-op logger is
// substituted). The gate starts set, so a worker is not hijacked by default.
func NewHijackable(logger *slog.Logger) *Hijackable {
	if logger == nil {
		logger = slog.New(slog.DiscardHandler)
	}
	return &Hijackable{
		gate:         newGate(),
		lastProgress: time.Now(),
		logger:       logger,
	}
}

// AwaitIfHijacked blocks the automation while a human is hijacking this worker.
//
// Call it at every checkpoint in the automation loop. It returns immediately
// when not hijacked or when a step token is available (consuming one),
// otherwise it blocks until SetHijacked(false) or ctx cancellation. Port of
// await_if_hijacked.
func (h *Hijackable) AwaitIfHijacked(ctx context.Context) error {
	h.mu.Lock()
	if !h.hijacked {
		h.mu.Unlock()
		return nil
	}
	if h.stepTokens > 0 {
		h.stepTokens--
		h.mu.Unlock()
		return nil
	}
	h.mu.Unlock()
	return h.gate.wait(ctx)
}

// SetHijacked pauses (true) or resumes (false) automation. It is idempotent.
// Port of set_hijacked.
func (h *Hijackable) SetHijacked(enabled bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if enabled == h.hijacked {
		return
	}
	h.hijacked = enabled
	if enabled {
		h.stepTokens = 0
		h.gate.clear()
	} else {
		h.gate.set()
	}
}

// RequestStep allows automation to pass checkpoints hijack gates while still
// hijacked. Two tokens (the Python default) let one full loop iteration run;
// tokens are capped at 100 and a non-positive argument is treated as zero. It
// is a no-op when not hijacked. Port of request_step.
//
// Like Python, RequestStep does not wake a checkpoint that is already blocked
// on the gate: tokens are consumed at the next checkpoint reached while the
// automation is running.
func (h *Hijackable) RequestStep(checkpoints int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.hijacked {
		return
	}
	if checkpoints < 0 {
		checkpoints = 0
	}
	h.stepTokens = min(h.stepTokens+checkpoints, 100)
}

// IsHijacked reports whether the worker is currently paused.
func (h *Hijackable) IsHijacked() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.hijacked
}

// StepTokens returns the number of buffered step tokens (test/introspection).
func (h *Hijackable) StepTokens() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.stepTokens
}

// NoteProgress resets the watchdog timer, signalling meaningful work. Port of
// note_progress.
func (h *Hijackable) NoteProgress() {
	h.mu.Lock()
	h.lastProgress = time.Now()
	h.mu.Unlock()
}

// StartWatchdog starts a background goroutine that fires opts.OnStuck if no
// NoteProgress call has been seen for opts.StuckTimeout. While hijacked, the
// timer is suppressed. Calling it again while a watchdog runs is a no-op. Port
// of start_watchdog.
func (h *Hijackable) StartWatchdog(opts WatchdogOptions) {
	if opts.StuckTimeout <= 0 {
		opts.StuckTimeout = 120 * time.Second
	}
	if opts.CheckInterval <= 0 {
		opts.CheckInterval = 5 * time.Second
	}
	if opts.CheckInterval < 500*time.Millisecond {
		opts.CheckInterval = 500 * time.Millisecond
	}

	h.mu.Lock()
	if h.watchdogCancel != nil {
		h.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	h.watchdogCancel = cancel
	h.watchdogDone = done
	h.mu.Unlock()

	go h.watchdogLoop(ctx, done, opts)
}

func (h *Hijackable) watchdogLoop(ctx context.Context, done chan struct{}, opts WatchdogOptions) {
	defer close(done)
	ticker := time.NewTicker(opts.CheckInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		h.mu.Lock()
		hijacked := h.hijacked
		last := h.lastProgress
		h.mu.Unlock()
		if hijacked {
			h.NoteProgress()
			continue
		}
		if time.Since(last) < opts.StuckTimeout {
			continue
		}
		if opts.OnStuck != nil {
			h.callOnStuck(opts.OnStuck)
		}
		// Reset so we do not spam if reconnect is slow.
		h.NoteProgress()
	}
}

// callOnStuck invokes the callback, recovering and logging any panic (mirroring
// the Python except-and-log around the on_stuck callback).
func (h *Hijackable) callOnStuck(fn func()) {
	defer func() {
		if r := recover(); r != nil {
			h.logger.Warn("watchdog on_stuck callback raised", "panic", r)
		}
	}()
	fn()
}

// StopWatchdog cancels the watchdog goroutine and waits for it to exit. It is
// idempotent. Port of stop_watchdog.
func (h *Hijackable) StopWatchdog() {
	h.mu.Lock()
	cancel := h.watchdogCancel
	done := h.watchdogDone
	h.watchdogCancel = nil
	h.watchdogDone = nil
	h.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	if done != nil {
		<-done
	}
}

// CleanupHijack releases the hijack and stops the watchdog. Call from a
// worker's cleanup/shutdown path. Port of cleanup_hijack.
func (h *Hijackable) CleanupHijack() {
	h.SetHijacked(false)
	h.StopWatchdog()
}
