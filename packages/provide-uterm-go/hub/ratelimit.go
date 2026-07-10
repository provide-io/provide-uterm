//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

// TokenBucket is a simple token-bucket rate limiter. Port of
// provide.uterm.server.bridge.ratelimit.TokenBucket.
//
// It is not internally synchronised: the Python original relies on the single
// event loop, and this port relies on the composing [RateLimiter] holding its
// mutex across every Allow call. Standalone use must serialise Allow calls.
type TokenBucket struct {
	rate       float64
	burst      float64
	tokens     float64
	lastRefill float64
	clock      Clock
}

// NewTokenBucket builds a bucket refilling at ratePerSec tokens/sec. burst is
// the max burst size (nil defaults to ratePerSec, i.e. one second of burst).
// clock supplies the monotonic reading; nil selects the real clock.
func NewTokenBucket(ratePerSec float64, burst *float64, clock Clock) *TokenBucket {
	clock = orDefaultClock(clock)
	b := ratePerSec
	if burst != nil {
		b = *burst
	}
	return &TokenBucket{
		rate:       ratePerSec,
		burst:      b,
		tokens:     b,
		lastRefill: clock.Monotonic(),
		clock:      clock,
	}
}

// Allow consumes one token if available, returning true if the request is
// admitted. Tokens refill by elapsed*rate (capped at burst) since the last call.
func (b *TokenBucket) Allow() bool {
	now := b.clock.Monotonic()
	elapsed := now - b.lastRefill
	b.tokens = minFloat(b.burst, b.tokens+elapsed*b.rate)
	b.lastRefill = now
	if b.tokens >= 1.0 {
		b.tokens -= 1.0
		return true
	}
	return false
}

func minFloat(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

func maxFloat(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
