//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"fmt"
	"testing"
)

// drain forces a bucket empty and freezes its refill clock into the future so
// the next Allow deterministically denies (mirrors the Python _drain helper).
func drain(b *TokenBucket, clk *ManualClock) {
	b.tokens = 0
	b.lastRefill = clk.Monotonic() + 3600
}

func newLimiter(clk *ManualClock) *RateLimiter {
	return NewRateLimiter(5.0, 20.0, clk)
}

func TestLimiterInitClampsRatesToMinimum(t *testing.T) {
	l := NewRateLimiter(0.0, -5.0, NewManualClock(0))
	mustEqual(t, l.AcquireRate(), MinRatePerSec, "acquire rate floor")
	mustEqual(t, l.SendRate(), MinRatePerSec, "send rate floor")
}

func TestLimiterInitPreservesNormalRates(t *testing.T) {
	l := NewRateLimiter(5.0, 20.0, NewManualClock(0))
	mustEqual(t, l.AcquireRate(), 5.0, "acquire rate")
	mustEqual(t, l.SendRate(), 20.0, "send rate")
	mustEqual(t, l.acquirePerClient.len(), 0, "empty per-client acquire")
	mustEqual(t, l.sendPerClient.len(), 0, "empty per-client send")
}

func TestLimiterAcquireAdmitsFirstRequest(t *testing.T) {
	l := newLimiter(NewManualClock(0))
	mustTrue(t, l.AllowRESTAcquire("client-a"), "first admitted")
	mustTrue(t, l.acquirePerClient.contains("client-a"), "per-client entry created")
}

func TestLimiterSendAdmitsFirstRequest(t *testing.T) {
	l := newLimiter(NewManualClock(0))
	mustTrue(t, l.AllowRESTSend("client-a"), "first admitted")
	mustTrue(t, l.sendPerClient.contains("client-a"), "per-client entry created")
}

func TestLimiterAcquirePerClientIsolation(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	l.AllowRESTAcquire("client-a")
	drain(l.acquirePerClient.get("client-a"), clk)
	mustFalse(t, l.AllowRESTAcquire("client-a"), "a drained -> deny")
	mustTrue(t, l.AllowRESTAcquire("client-b"), "b isolated -> allow")
	mustTrue(t, l.acquirePerClient.get("client-a") != l.acquirePerClient.get("client-b"), "distinct buckets")
}

func TestLimiterSendPerClientIsolation(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	l.AllowRESTSend("c1")
	drain(l.sendPerClient.get("c1"), clk)
	mustFalse(t, l.AllowRESTSend("c1"), "c1 drained")
	mustTrue(t, l.AllowRESTSend("c2"), "c2 allowed")
}

func TestLimiterAcquireGlobalBucketDeniesAfterPerClientPass(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	drain(l.acquireBucket, clk)
	mustFalse(t, l.AllowRESTAcquire("client-a"), "global drained denies")
}

func TestLimiterSendGlobalBucketDeniesAfterPerClientPass(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	drain(l.sendBucket, clk)
	mustFalse(t, l.AllowRESTSend("client-a"), "global drained denies")
}

func TestLimiterAcquireShortCircuitSkipsGlobalWhenPerClientDenies(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	pre := NewTokenBucket(5.0, nil, clk)
	l.acquirePerClient.set("client-a", pre)
	drain(pre, clk)
	before := l.acquireBucket.tokens
	mustFalse(t, l.AllowRESTAcquire("client-a"), "per-client denies")
	mustEqual(t, l.acquireBucket.tokens, before, "global untouched")
}

func TestLimiterSendShortCircuitSkipsGlobalWhenPerClientDenies(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	pre := NewTokenBucket(20.0, nil, clk)
	l.sendPerClient.set("client-a", pre)
	drain(pre, clk)
	before := l.sendBucket.tokens
	mustFalse(t, l.AllowRESTSend("client-a"), "per-client denies")
	mustEqual(t, l.sendBucket.tokens, before, "global untouched")
}

func seedBuckets(l *lruBuckets, n int, rate float64, clk *ManualClock) {
	for i := 0; i < n; i++ {
		l.set(fmt.Sprintf("c%d", i), NewTokenBucket(rate, nil, clk))
	}
}

func TestLimiterAcquireEvictionAtCapDropsOldestHalf(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	seedBuckets(l.acquirePerClient, RESTClientCacheMax, 5.0, clk)
	mustEqual(t, l.acquirePerClient.len(), RESTClientCacheMax, "seeded to cap")
	l.AllowRESTAcquire("new-client")
	expected := RESTClientCacheMax - RESTClientEvictCount + 1
	mustEqual(t, l.acquirePerClient.len(), expected, "len after eviction")
	mustTrue(t, l.acquirePerClient.contains("new-client"), "new kept")
	mustFalse(t, l.acquirePerClient.contains("c0"), "oldest evicted")
	mustTrue(t, l.acquirePerClient.contains(fmt.Sprintf("c%d", RESTClientCacheMax-1)), "newest kept")
}

func TestLimiterSendEvictionAtCapDropsOldestHalf(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	seedBuckets(l.sendPerClient, RESTClientCacheMax, 20.0, clk)
	l.AllowRESTSend("new-client")
	expected := RESTClientCacheMax - RESTClientEvictCount + 1
	mustEqual(t, l.sendPerClient.len(), expected, "len after eviction")
	mustTrue(t, l.sendPerClient.contains("new-client"), "new kept")
	mustFalse(t, l.sendPerClient.contains("c0"), "oldest evicted")
}

func TestLimiterAcquireEvictionNeverDropsInsertingClient(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	seedBuckets(l.acquirePerClient, RESTClientCacheMax, 5.0, clk)
	drain(l.acquirePerClient.get("c0"), clk)
	// The victim (oldest, drained) forces the overflow — but touching an
	// existing key does not grow the map, so no eviction runs and it survives.
	mustFalse(t, l.AllowRESTAcquire("c0"), "drained victim denied, not reset")
	mustTrue(t, l.acquirePerClient.contains("c0"), "victim kept")
	mustTrue(t, l.acquirePerClient.get("c0").tokens < 1.0, "victim still drained")
}

func TestLimiterSendEvictionNeverDropsInsertingClient(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	seedBuckets(l.sendPerClient, RESTClientCacheMax, 20.0, clk)
	drain(l.sendPerClient.get("c0"), clk)
	mustFalse(t, l.AllowRESTSend("c0"), "drained victim denied")
	mustTrue(t, l.sendPerClient.contains("c0"), "victim kept")
}

func TestLimiterAcquireAccessMovesClientToEndLRU(t *testing.T) {
	clk := NewManualClock(0)
	l := newLimiter(clk)
	seedBuckets(l.acquirePerClient, RESTClientCacheMax, 5.0, clk)
	l.AllowRESTAcquire("c0")         // refresh recency
	l.AllowRESTAcquire("new-client") // triggers overflow eviction
	mustTrue(t, l.acquirePerClient.contains("c0"), "refreshed kept")
	mustFalse(t, l.acquirePerClient.contains("c1"), "now-oldest evicted")
	mustTrue(t, l.acquirePerClient.contains("new-client"), "new kept")
}

func TestEvictIfFullNoopAtExactCap(t *testing.T) {
	clk := NewManualClock(0)
	lb := newLRUBuckets()
	seedBuckets(lb, RESTClientCacheMax, 5.0, clk)
	lb.evictIfFull()
	mustEqual(t, lb.len(), RESTClientCacheMax, "noop at cap")
	mustTrue(t, lb.contains("c0"), "c0 kept")
	mustTrue(t, lb.contains(fmt.Sprintf("c%d", RESTClientCacheMax-1)), "newest kept")
}

func TestEvictIfFullDropsOldestHalfOnOverflow(t *testing.T) {
	clk := NewManualClock(0)
	lb := newLRUBuckets()
	seedBuckets(lb, RESTClientCacheMax+1, 5.0, clk)
	lb.evictIfFull()
	mustEqual(t, lb.len(), RESTClientCacheMax+1-RESTClientEvictCount, "trimmed oldest half")
	mustFalse(t, lb.contains("c0"), "c0 evicted")
	mustTrue(t, lb.contains(fmt.Sprintf("c%d", RESTClientEvictCount)), "boundary kept")
}

func TestLRUSetReplacesExistingKeyRecency(t *testing.T) {
	clk := NewManualClock(0)
	lb := newLRUBuckets()
	b1 := NewTokenBucket(5, nil, clk)
	b2 := NewTokenBucket(5, nil, clk)
	lb.set("x", b1)
	lb.set("x", b2) // replace: must not duplicate order entry
	mustEqual(t, lb.len(), 1, "single entry")
	mustTrue(t, lb.get("x") == b2, "replaced bucket")
	mustEqual(t, len(lb.order), 1, "no duplicate order entry")
}

func TestLimiterDefaultClock(t *testing.T) {
	l := NewRateLimiter(5.0, 20.0, nil) // nil clock -> real clock
	mustTrue(t, l.AllowRESTAcquire("x"), "real-clock limiter admits")
}
