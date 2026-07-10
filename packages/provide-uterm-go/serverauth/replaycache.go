//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

// replayCacheMaxEntries ports auth_webhook._REPLAY_CACHE_MAX_ENTRIES.
const replayCacheMaxEntries = 4096

// boundedReplayCache ports auth_webhook._BoundedReplayCache — a bounded,
// TTL-evicted cache of recently-seen response signatures (L9 replay
// protection, layer 1). Not safe for concurrent use on its own; the webhook
// provider serialises access.
type boundedReplayCache struct {
	maxAgeS    float64
	maxEntries int
	order      []string           // FIFO insertion order (oldest at front)
	seen       map[string]float64 // signature -> first-seen wall-clock timestamp
}

func newBoundedReplayCache(maxAgeS float64, maxEntries int) *boundedReplayCache {
	return &boundedReplayCache{maxAgeS: maxAgeS, maxEntries: maxEntries, seen: map[string]float64{}}
}

func (c *boundedReplayCache) len() int { return len(c.seen) }

func (c *boundedReplayCache) contains(sig string) bool {
	_, ok := c.seen[sig]
	return ok
}

// purgeStale removes entries older than the freshness window; entries are in
// time order so it stops at the first still-live entry.
func (c *boundedReplayCache) purgeStale(now float64) {
	for len(c.order) > 0 {
		front := c.order[0]
		if now-c.seen[front] > c.maxAgeS {
			delete(c.seen, front)
			c.order = c.order[1:]
		} else {
			break
		}
	}
}

// seenOrRecord ports seen_or_record: return true if sig is a replay; otherwise
// record it.
func (c *boundedReplayCache) seenOrRecord(sig string, now float64) bool {
	c.purgeStale(now)
	if ts, ok := c.seen[sig]; ok && now-ts <= c.maxAgeS {
		return true
	}
	c.seen[sig] = now
	c.order = append(c.order, sig)
	for len(c.seen) > c.maxEntries {
		oldest := c.order[0]
		c.order = c.order[1:]
		delete(c.seen, oldest)
	}
	return false
}
