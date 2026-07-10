//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

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
