//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

// TestMinRatePerSecIsTheLimiterFloor pins the tightest rate any bucket-backed
// policy may be configured with, and that the limiter clamps to exactly it.
// serverconfig refuses anything below this value, so the two must not drift:
// were the floor to rise without the config check following, an accepted rate
// would be silently clamped looser than the operator wrote.
func TestMinRatePerSecIsTheLimiterFloor(t *testing.T) {
	mustEqual(t, MinRatePerSec, 1.0, "MinRatePerSec")
	l := NewRateLimiter(MinRatePerSec/2, MinRatePerSec/2, NewManualClock(0))
	mustEqual(t, l.AcquireRate(), MinRatePerSec, "acquire clamped to the floor")
	mustEqual(t, l.SendRate(), MinRatePerSec, "send clamped to the floor")
}

// TestTokenBucketBelowFloorAdmitsNothingEver is why the floor is 1.0 and not
// something smaller. Burst is one second of the rate, so a bucket built below
// 1/sec caps its own tokens below the single token a call costs: it denies
// every call forever, however long the caller waits. A rate in that band is
// not a tight limit, it is a bricked endpoint — which is why the config
// refuses one. This test is the guard against the floor being lowered again.
func TestTokenBucketBelowFloorAdmitsNothingEver(t *testing.T) {
	for _, rate := range []float64{0.1, 0.5, 0.99} {
		clk := NewManualClock(0)
		b := NewTokenBucket(rate, nil, clk)
		// A day of refill at a rate that can never fill one token.
		for _, elapsed := range []float64{0, 1, 10, 3600, 86400} {
			clk.SetMonotonic(elapsed)
			if b.Allow() {
				t.Errorf("rate %v admitted a call after %vs; a sub-1/sec bucket must never admit", rate, elapsed)
			}
		}
	}
}

// TestTokenBucketAtFloorAdmitsImmediately is the other half: exactly at the
// floor the bucket holds one whole token, so the first call is admitted. One
// call per second is the tightest limit a bucket whose burst equals its rate
// can actually honour.
func TestTokenBucketAtFloorAdmitsImmediately(t *testing.T) {
	b := NewTokenBucket(MinRatePerSec, nil, NewManualClock(0))
	mustTrue(t, b.Allow(), "a bucket at the floor admits its first call")
}

func TestTokenBucketConsumesBurstThenDenies(t *testing.T) {
	clk := NewManualClock(0)
	clk.SetMonotonic(100)
	b := NewTokenBucket(5, nil, clk)
	for i := 0; i < 5; i++ {
		mustTrue(t, b.Allow(), "allow within burst")
	}
	mustFalse(t, b.Allow(), "deny when drained")
}

func TestTokenBucketRefillsOverTime(t *testing.T) {
	clk := NewManualClock(0)
	clk.SetMonotonic(100)
	b := NewTokenBucket(5, nil, clk)
	for i := 0; i < 5; i++ {
		b.Allow()
	}
	mustFalse(t, b.Allow(), "drained")
	clk.SetMonotonic(101) // +1s -> +5 tokens (capped at burst 5)
	mustTrue(t, b.Allow(), "refilled after 1s")
}

func TestTokenBucketExplicitBurst(t *testing.T) {
	clk := NewManualClock(0)
	clk.SetMonotonic(100)
	b := NewTokenBucket(5, f64p(2), clk)
	mustTrue(t, b.Allow(), "burst 1")
	mustTrue(t, b.Allow(), "burst 2")
	mustFalse(t, b.Allow(), "burst exhausted")
}

func TestTokenBucketDefaultClock(t *testing.T) {
	// nil clock selects the real clock; a fresh bucket admits its burst.
	b := NewTokenBucket(3, nil, nil)
	mustTrue(t, b.Allow(), "real-clock bucket admits")
}
