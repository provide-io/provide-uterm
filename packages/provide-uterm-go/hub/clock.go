//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"sync"
	"time"
)

// Clock abstracts the two time sources the Python services read
// (time.monotonic and time.time) plus a cancellable sleep used by the polling
// coordinator. Injecting a Clock keeps every time-dependent service
// deterministic in tests.
//
// Monotonic returns a strictly non-decreasing seconds value (the analogue of
// Python's time.monotonic); Wall returns Unix wall-clock seconds (time.time).
// Sleep blocks for the given number of seconds or until ctx is cancelled.
type Clock interface {
	Monotonic() float64
	Wall() float64
	Sleep(ctx context.Context, seconds float64) error
}

// realClock is the production [Clock]. Monotonic is measured against a base
// captured at construction so the returned value is a real monotonic reading.
type realClock struct{ base time.Time }

// NewRealClock returns a [Clock] backed by the process wall + monotonic clock.
func NewRealClock() Clock { return &realClock{base: time.Now()} }

func (c *realClock) Monotonic() float64 { return time.Since(c.base).Seconds() }

func (c *realClock) Wall() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func (c *realClock) Sleep(ctx context.Context, seconds float64) error {
	if seconds <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(time.Duration(seconds * float64(time.Second)))
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// orDefaultClock returns c, or a fresh real clock when c is nil, so callers can
// pass nil to opt into the production clock.
func orDefaultClock(c Clock) Clock {
	if c == nil {
		return NewRealClock()
	}
	return c
}

// ManualClock is a deterministic [Clock] for tests and for wave-B composition
// that wants to drive time explicitly. Monotonic and Wall return the values
// last set; Sleep records its argument and advances Monotonic by Step (default
// 1.0), mirroring the fake clock used by the Python polling suite.
type ManualClock struct {
	mu             sync.Mutex
	mono           float64
	wall           float64
	step           float64
	sleepsRecorded []float64
}

// NewManualClock returns a ManualClock starting at monotonic 0 and the given
// wall time, advancing monotonic by 1.0 per Sleep.
func NewManualClock(wall float64) *ManualClock {
	return &ManualClock{wall: wall, step: 1.0}
}

// SetStep overrides the per-Sleep monotonic advance.
func (c *ManualClock) SetStep(step float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.step = step
}

// SetMonotonic overrides the current monotonic value.
func (c *ManualClock) SetMonotonic(v float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.mono = v
}

// SetWall overrides the current wall value.
func (c *ManualClock) SetWall(v float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.wall = v
}

// Monotonic implements [Clock].
func (c *ManualClock) Monotonic() float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.mono
}

// Wall implements [Clock].
func (c *ManualClock) Wall() float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.wall
}

// Sleep records seconds and advances monotonic by Step; it never blocks.
func (c *ManualClock) Sleep(_ context.Context, seconds float64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sleepsRecorded = append(c.sleepsRecorded, seconds)
	c.mono += c.step
	return nil
}

// Sleeps returns a copy of the recorded Sleep arguments in call order.
func (c *ManualClock) Sleeps() []float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]float64, len(c.sleepsRecorded))
	copy(out, c.sleepsRecorded)
	return out
}
