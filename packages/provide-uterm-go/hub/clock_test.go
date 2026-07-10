//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"testing"
	"time"
)

func TestRealClockMonotonicIncreasesAndWall(t *testing.T) {
	c := NewRealClock()
	m0 := c.Monotonic()
	time.Sleep(time.Millisecond)
	mustTrue(t, c.Monotonic() >= m0, "monotonic non-decreasing")
	mustTrue(t, c.Wall() > 0, "wall positive")
}

func TestRealClockSleep(t *testing.T) {
	c := NewRealClock()
	mustTrue(t, c.Sleep(context.Background(), 0.001) == nil, "short sleep completes")

	// Non-positive sleep returns immediately (ctx err, nil here).
	mustTrue(t, c.Sleep(context.Background(), 0) == nil, "zero sleep no error")

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	mustTrue(t, c.Sleep(ctx, 10) != nil, "cancelled sleep errors")
}

func TestManualClock(t *testing.T) {
	c := NewManualClock(5000)
	mustEqual(t, c.Wall(), 5000.0, "wall")
	mustEqual(t, c.Monotonic(), 0.0, "mono starts 0")
	c.SetMonotonic(100)
	c.SetWall(200)
	c.SetStep(2)
	mustEqual(t, c.Monotonic(), 100.0, "set mono")
	mustEqual(t, c.Wall(), 200.0, "set wall")
	mustTrue(t, c.Sleep(context.Background(), 0.5) == nil, "sleep no error")
	mustEqual(t, c.Monotonic(), 102.0, "advanced by step")
	mustDeepEqual(t, c.Sleeps(), []float64{0.5}, "recorded sleep arg")
}

func TestOrDefaultClock(t *testing.T) {
	c := NewManualClock(1)
	mustTrue(t, orDefaultClock(c) == c, "passes through non-nil")
	mustTrue(t, orDefaultClock(nil) != nil, "nil -> real clock")
}

func TestLoggerOrDefault(t *testing.T) {
	l := discardLogger()
	mustTrue(t, loggerOrDefault(l) == l, "passthrough")
	mustTrue(t, loggerOrDefault(nil) != nil, "nil -> default")
}
