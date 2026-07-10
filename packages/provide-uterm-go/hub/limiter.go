//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "sync"

// Per-client rate-limit bucket cache bounds. Port of the module constants in
// provide.uterm.server.bridge.hub.limiter. On overflow the oldest (least
// recently used) half is evicted, preserving state for recently active
// clients while bounding memory.
const (
	RESTClientCacheMax   = 1024
	RESTClientEvictCount = RESTClientCacheMax / 2
)

// lruBuckets is an insertion/recency-ordered map of per-client token buckets
// implementing the Python dict-with-reinsert LRU semantics. order is oldest →
// newest; touching a key moves it to the newest end.
type lruBuckets struct {
	order []string
	m     map[string]*TokenBucket
}

func newLRUBuckets() *lruBuckets {
	return &lruBuckets{m: map[string]*TokenBucket{}}
}

func (l *lruBuckets) removeFromOrder(key string) {
	for i, k := range l.order {
		if k == key {
			l.order = append(l.order[:i], l.order[i+1:]...)
			return
		}
	}
}

// touch mirrors RateLimiter._touch: get-or-create the client's bucket, move it
// to the most-recent end (preserving an existing bucket's state), then evict.
// Eviction runs AFTER the reinsert and never drops the key just touched.
func (l *lruBuckets) touch(key string, rate float64, clock Clock) *TokenBucket {
	b, ok := l.m[key]
	if ok {
		delete(l.m, key)
		l.removeFromOrder(key)
	} else {
		b = NewTokenBucket(rate, nil, clock)
	}
	l.m[key] = b
	l.order = append(l.order, key)
	l.evictIfFull()
	return b
}

// evictIfFull drops the oldest RESTClientEvictCount entries once the cache
// exceeds the cap. Insertion order is recency order, so trimming the front is
// a true LRU eviction. Mirrors RateLimiter._evict_if_full.
func (l *lruBuckets) evictIfFull() {
	if len(l.m) > RESTClientCacheMax {
		victims := l.order[:RESTClientEvictCount]
		for _, k := range victims {
			delete(l.m, k)
		}
		// Copy the surviving tail into a fresh slice so the evicted keys'
		// backing storage can be reclaimed and there is no aliasing.
		rest := l.order[RESTClientEvictCount:]
		l.order = append(make([]string, 0, len(rest)), rest...)
	}
}

// set inserts a bucket directly at the most-recent end (used to seed the cache;
// mirrors assigning entries into the Python per-client dict).
func (l *lruBuckets) set(key string, b *TokenBucket) {
	if _, ok := l.m[key]; ok {
		l.removeFromOrder(key)
	}
	l.m[key] = b
	l.order = append(l.order, key)
}

func (l *lruBuckets) contains(key string) bool { _, ok := l.m[key]; return ok }

func (l *lruBuckets) get(key string) *TokenBucket { return l.m[key] }

func (l *lruBuckets) len() int { return len(l.m) }

// RateLimiter composes per-purpose token buckets for hub REST endpoints. Port
// of provide.uterm.server.bridge.hub.limiter.RateLimiter.
//
// Two purposes are tracked: REST hijack-acquire and REST send. Each has a
// global bucket plus an LRU-capped per-client bucket cache; both the
// per-client and global bucket must allow for a request to be admitted
// (per-client consumed first; if it denies, the global is not consumed).
//
// Deviation: the Python limiter holds no locks (relying on the event loop).
// This port guards all bucket state with a mutex so it is safe under -race.
type RateLimiter struct {
	mu sync.Mutex

	acquireRate float64
	sendRate    float64

	acquireBucket *TokenBucket
	sendBucket    *TokenBucket

	acquirePerClient *lruBuckets
	sendPerClient    *lruBuckets

	clock Clock
}

// NewRateLimiter builds a limiter with the given acquire/send token rates
// (each clamped up to a 0.1/sec floor to avoid divide-by-zero / stuck
// buckets). clock is injectable; nil selects the real clock.
func NewRateLimiter(restAcquireRate, restSendRate float64, clock Clock) *RateLimiter {
	clock = orDefaultClock(clock)
	acquireRate := maxFloat(0.1, restAcquireRate)
	sendRate := maxFloat(0.1, restSendRate)
	return &RateLimiter{
		acquireRate:      acquireRate,
		sendRate:         sendRate,
		acquireBucket:    NewTokenBucket(acquireRate, nil, clock),
		sendBucket:       NewTokenBucket(sendRate, nil, clock),
		acquirePerClient: newLRUBuckets(),
		sendPerClient:    newLRUBuckets(),
		clock:            clock,
	}
}

// AcquireRate returns the configured tokens/sec for the REST acquire policy.
func (rl *RateLimiter) AcquireRate() float64 { return rl.acquireRate }

// SendRate returns the configured tokens/sec for the REST send policy.
func (rl *RateLimiter) SendRate() float64 { return rl.sendRate }

// AllowRESTAcquire reports whether clientID passes both the global and
// per-client acquire limits. Short-circuits: if the per-client bucket denies,
// the global bucket is not consumed.
func (rl *RateLimiter) AllowRESTAcquire(clientID string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	bucket := rl.acquirePerClient.touch(clientID, rl.acquireRate, rl.clock)
	if !bucket.Allow() {
		return false
	}
	return rl.acquireBucket.Allow()
}

// AllowRESTSend reports whether clientID passes both the global and per-client
// send limits, with the same composition and LRU eviction as AllowRESTAcquire.
func (rl *RateLimiter) AllowRESTSend(clientID string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	bucket := rl.sendPerClient.touch(clientID, rl.sendRate, rl.clock)
	if !bucket.Allow() {
		return false
	}
	return rl.sendBucket.Allow()
}
