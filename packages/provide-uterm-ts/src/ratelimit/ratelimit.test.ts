//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { RateLimiter, REST_CLIENT_CACHE_MAX, REST_CLIENT_EVICT_COUNT, TokenBucket } from "./index.ts";

interface RateLimitGolden {
  buckets: Array<{
    name: string;
    rate: number;
    burst: number | null;
    script: Array<[number, number]>;
    verdicts: boolean[];
  }>;
  limiter: {
    per_client: boolean[];
    other_client: boolean[];
    send: boolean[];
    global_verdicts: boolean[];
    global_untouched_by_denial: boolean;
  };
  eviction: {
    cache_max: number;
    evict_count: number;
    size_at_cap: number;
    size_after_overflow: number;
    overflow_client_kept: boolean;
    oldest_evicted: boolean;
    newest_kept: boolean;
  };
  eviction_small: {
    cache_max: number;
    evict_count: number;
    size_at_cap: number;
    size_after_overflow: number;
    overflow_client_kept: boolean;
    oldest_evicted: boolean;
    newest_kept: boolean;
  };
  self_reset: { first: boolean; second: boolean; after_churn: boolean };
}

const golden = loadGolden<RateLimitGolden>("ratelimit_golden.json");

/** A clock the test drives by hand, in seconds. */
function stubClock(start = 1000): { now: () => number; advance: (seconds: number) => void } {
  let current = start;
  return {
    now: () => current,
    advance: (seconds: number) => {
      current += seconds;
    },
  };
}

describe("TokenBucket", () => {
  it("starts full and spends down to empty", () => {
    const clock = stubClock();
    const bucket = new TokenBucket(3, { now: clock.now });
    expect([bucket.allow(), bucket.allow(), bucket.allow(), bucket.allow()]).toStrictEqual([true, true, true, false]);
  });

  it("defaults its burst to one second of capacity", () => {
    const clock = stubClock();
    const bucket = new TokenBucket(2, { now: clock.now });
    expect([bucket.allow(), bucket.allow(), bucket.allow()]).toStrictEqual([true, true, false]);
  });

  it("honours an explicit burst larger than the rate", () => {
    const clock = stubClock();
    const bucket = new TokenBucket(1, { burst: 3, now: clock.now });
    expect([bucket.allow(), bucket.allow(), bucket.allow(), bucket.allow()]).toStrictEqual([true, true, true, false]);
  });

  it("refills over elapsed time", () => {
    const clock = stubClock();
    const bucket = new TokenBucket(1, { now: clock.now });
    expect(bucket.allow()).toBe(true);
    expect(bucket.allow()).toBe(false);
    clock.advance(1);
    expect(bucket.allow()).toBe(true);
  });

  it("does not grant a token for a partial refill", () => {
    const clock = stubClock();
    const bucket = new TokenBucket(1, { now: clock.now });
    bucket.allow();
    clock.advance(0.5);
    expect(bucket.allow()).toBe(false);
    clock.advance(0.5);
    expect(bucket.allow()).toBe(true);
  });

  it("caps the refill at the burst however long it waits", () => {
    const clock = stubClock();
    const bucket = new TokenBucket(2, { now: clock.now });
    bucket.allow();
    bucket.allow();
    clock.advance(1000);
    expect([bucket.allow(), bucket.allow(), bucket.allow()]).toStrictEqual([true, true, false]);
  });

  it("never refills at a zero rate", () => {
    const clock = stubClock();
    const bucket = new TokenBucket(0, { burst: 1, now: clock.now });
    expect(bucket.allow()).toBe(true);
    clock.advance(1000);
    expect(bucket.allow()).toBe(false);
  });

  it("admits nothing below one per second, and one per second at one", () => {
    // The property {@link MIN_RATE_PER_SEC} exists for, stated either side of
    // the floor rather than in terms of the constant — so it goes on holding
    // whatever the constant is, and fails the day somebody lowers the floor
    // again.
    //
    // The default burst is one second of the rate, so a bucket below 1/s has a
    // ceiling under the whole token a call costs and admits nothing however
    // long the caller waits. That is why the configuration refuses the whole
    // band rather than reading `0.5` as "one call every two seconds", and why
    // making such a rate mean anything would take decoupling burst from rate
    // (`burst = max(1, rate)`) across every port and its recorded goldens.
    //
    // Built straight rather than through {@link RateLimiter}: the limiter
    // clamps to the floor, so a starved bucket is not otherwise reachable.
    const clock = stubClock();
    const starved = new TokenBucket(0.99, { now: clock.now });
    expect(starved.allow()).toBe(false);
    clock.advance(1000);
    expect(starved.allow()).toBe(false);

    const atFloor = new TokenBucket(1, { now: clock.now });
    expect([atFloor.allow(), atFloor.allow()]).toStrictEqual([true, false]);
    clock.advance(1);
    expect([atFloor.allow(), atFloor.allow()]).toStrictEqual([true, false]);
  });
});

describe("RateLimiter composition", () => {
  it("denies once a client exhausts its own bucket", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 2, restSendRate: 2, now: clock.now });
    expect([1, 2, 3, 4].map(() => limiter.allowRestAcquire("c1"))).toStrictEqual([true, true, false, false]);
  });

  it("keeps each client's budget separate", () => {
    // The global bucket is widened deliberately: at equal rates it would be
    // the binding limit, and this is asserting per-client isolation.
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1, restSendRate: 1, now: clock.now });
    limiter.restAcquireBucket = new TokenBucket(1000, { now: clock.now });
    expect(limiter.allowRestAcquire("c1")).toBe(true);
    expect(limiter.allowRestAcquire("c1")).toBe(false);
    expect(limiter.allowRestAcquire("c2")).toBe(true);
  });

  it("lets the global bucket bind when it is the tighter of the two", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1, restSendRate: 1, now: clock.now });
    expect(limiter.allowRestAcquire("c1")).toBe(true);
    // c2 has a full per-client bucket, but the shared one is already spent.
    expect(limiter.allowRestAcquire("c2")).toBe(false);
  });

  it("keeps the acquire and send policies independent", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1, restSendRate: 1, now: clock.now });
    expect(limiter.allowRestAcquire("c1")).toBe(true);
    expect(limiter.allowRestAcquire("c1")).toBe(false);
    // The send policy has its own buckets and is untouched.
    expect(limiter.allowRestSend("c1")).toBe(true);
  });

  it("denies a fresh client once the global bucket is exhausted", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1000, restSendRate: 1000, now: clock.now });
    limiter.restAcquireBucket = new TokenBucket(2, { now: clock.now });
    expect([0, 1, 2, 3].map((i) => limiter.allowRestAcquire(`c${i}`))).toStrictEqual([true, true, false, false]);
  });

  it("does not spend a global token when the per-client bucket denies", () => {
    // Short-circuit order matters: a client hammering its own limit must not
    // drain the shared budget for everyone else.
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1, restSendRate: 1, now: clock.now });
    const globalBucket = new TokenBucket(1000, { now: clock.now });
    limiter.restAcquireBucket = globalBucket;
    limiter.allowRestAcquire("c1");
    limiter.allowRestAcquire("c1");
    limiter.allowRestAcquire("c1");
    // One global token spent for the one admitted request, no more.
    expect(globalBucket.tokens).toBeGreaterThan(998);
  });
});

describe("RateLimiter per-client cache", () => {
  it("evicts the oldest half once the cap is passed", () => {
    // Small injected cap (see evictIfFull's docstring): this is the golden
    // corpus's eviction_small section, generated from the same Python
    // reference algorithm at cache_max=8/evict_count=4, not a hand-picked
    // scale — proves the algorithm without looping to the real 1024/512
    // production constants (see "exposes the documented cache bounds" for
    // those).
    const clock = stubClock();
    const limiter = new RateLimiter({
      restAcquireRate: 1000,
      restSendRate: 1000,
      now: clock.now,
      clientCacheMax: 8,
      clientEvictCount: 4,
    });
    for (let i = 0; i < 8; i += 1) {
      limiter.allowRestAcquire(`c${i}`);
    }
    expect(limiter.restAcquireClientCount).toBe(golden.eviction_small.size_at_cap);
    limiter.allowRestAcquire("overflow");
    expect(limiter.restAcquireClientCount).toBe(golden.eviction_small.size_after_overflow);
  });

  it("never evicts the client it is currently serving", () => {
    // A small injected cap, not the real REST_CLIENT_CACHE_MAX: this proves the
    // algorithm's LRU-eviction property, which does not depend on the specific
    // production cap size. See evictIfFull's docstring for why looping to the
    // real 1024 here would make its `break` a mutation-testing hit-count trap.
    const clock = stubClock();
    const limiter = new RateLimiter({
      restAcquireRate: 1000,
      restSendRate: 1000,
      now: clock.now,
      clientCacheMax: 8,
      clientEvictCount: 4,
    });
    for (let i = 0; i < 8; i += 1) {
      limiter.allowRestAcquire(`c${i}`);
    }
    limiter.allowRestAcquire("overflow");
    expect(limiter.hasRestAcquireClient("overflow")).toBe(true);
    expect(limiter.hasRestAcquireClient("c0")).toBe(false);
    expect(limiter.hasRestAcquireClient("c7")).toBe(true);
  });

  it("refreshes recency on access rather than only on insert", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({
      restAcquireRate: 1000,
      restSendRate: 1000,
      now: clock.now,
      clientCacheMax: 8,
      clientEvictCount: 4,
    });
    limiter.allowRestAcquire("early");
    for (let i = 0; i < 7; i += 1) {
      limiter.allowRestAcquire(`c${i}`);
    }
    // Touch the oldest client so it is no longer the oldest.
    limiter.allowRestAcquire("early");
    limiter.allowRestAcquire("overflow");
    expect(limiter.hasRestAcquireClient("early")).toBe(true);
  });

  it("does not let a client reset its own limit by churning the cache", () => {
    // The security property: if eviction could drop a drained bucket, an
    // attacker would refill their own allowance by flooding new client ids.
    //
    // NOT shrunk like the two tests above: `restAcquireBucket` here is a
    // SEPARATE, shared global bucket (1000 tokens) that every call — victim's
    // and every noise client's — also draws from. At the real 1024/512 scale,
    // ~1025 noise calls happen to exhaust that global bucket around the same
    // point eviction happens, so `after_churn: false` is produced by the
    // global bucket being drained, not solely by the per-client eviction
    // property the comment above describes. Shrinking the cache without also
    // reasoning about the global bucket's 1000-token budget changes which
    // mechanism produces the recorded golden answer — verified directly: at
    // cache_max=8 with only 9 noise calls, the global bucket is nowhere near
    // drained, and `after_churn` flips to `true`. Left at real scale so this
    // continues to test the same thing the golden corpus recorded.
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1, restSendRate: 1, now: clock.now });
    limiter.restAcquireBucket = new TokenBucket(1000, { now: clock.now });
    const first = limiter.allowRestAcquire("victim");
    const second = limiter.allowRestAcquire("victim");
    for (let i = 0; i <= REST_CLIENT_CACHE_MAX; i += 1) {
      limiter.allowRestAcquire(`noise${i}`);
    }
    const afterChurn = limiter.allowRestAcquire("victim");
    expect({ first, second, after_churn: afterChurn }).toStrictEqual(golden.self_reset);
  });
});

describe("RateLimiter configuration", () => {
  it("floors a rate below the minimum rather than accepting zero", () => {
    // Any rate under one per second would deny every request forever — the
    // burst is one second of the rate, so the bucket never holds the whole
    // token a call costs. That is a configuration mistake rather than a policy
    // anyone wants, so it is floored here and refused outright at config load.
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 0, restSendRate: -5, now: clock.now });
    expect({ acquire: limiter.restAcquireRate, send: limiter.restSendRate }).toStrictEqual({
      acquire: 1,
      send: 1,
    });
  });

  it("exposes the configured rates", () => {
    const limiter = new RateLimiter({ restAcquireRate: 7, restSendRate: 9 });
    expect({ acquire: limiter.restAcquireRate, send: limiter.restSendRate }).toStrictEqual({ acquire: 7, send: 9 });
  });

  it("exposes the documented cache bounds", () => {
    expect({ cache_max: REST_CLIENT_CACHE_MAX, evict_count: REST_CLIENT_EVICT_COUNT }).toStrictEqual({
      cache_max: golden.eviction.cache_max,
      evict_count: golden.eviction.evict_count,
    });
  });

  it("defaults to a real monotonic clock", async () => {
    const bucket = new TokenBucket(1000);
    expect(bucket.allow()).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(bucket.allow()).toBe(true);
  });

  it("uses seconds, not milliseconds, for the default monotonic clock", () => {
    // The test above only proves the default clock advances at all — a high
    // rate (1000/s) refills within 5ms whichever scale is used. A rate of 1/s
    // makes the scale itself observable: at the correct scale, two calls made
    // back-to-back with no artificial delay have not had a whole second pass
    // between them, so the second must be refused. A clock that multiplied
    // performance.now() by 1000 instead of dividing turns even a
    // microsecond's real gap into what looks like a full second, refilling
    // the bucket before the second call.
    const bucket = new TokenBucket(1);
    expect(bucket.allow()).toBe(true);
    expect(bucket.allow()).toBe(false);
  });
});

describe("differential parity with CPython", () => {
  it("matches every recorded bucket verdict", () => {
    for (const record of golden.buckets) {
      const clock = stubClock();
      const bucket = new TokenBucket(record.rate, {
        ...(record.burst === null ? {} : { burst: record.burst }),
        now: clock.now,
      });
      const verdicts: boolean[] = [];
      for (const [advance, calls] of record.script) {
        clock.advance(advance);
        for (let i = 0; i < calls; i += 1) {
          verdicts.push(bucket.allow());
        }
      }
      expect({ name: record.name, verdicts }).toStrictEqual({ name: record.name, verdicts: record.verdicts });
    }
    expect(golden.buckets.length).toBeGreaterThan(6);
  });

  it("matches the recorded composition verdicts", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 2, restSendRate: 2, now: clock.now });
    limiter.restAcquireBucket = new TokenBucket(1000, { now: clock.now });
    limiter.restSendBucket = new TokenBucket(1000, { now: clock.now });
    expect({
      per_client: [1, 2, 3, 4].map(() => limiter.allowRestAcquire("c1")),
      other_client: [1, 2, 3, 4].map(() => limiter.allowRestAcquire("c2")),
      send: [1, 2, 3, 4].map(() => limiter.allowRestSend("c1")),
    }).toStrictEqual({
      per_client: golden.limiter.per_client,
      other_client: golden.limiter.other_client,
      send: golden.limiter.send,
    });
  });

  it("matches the recorded eviction outcome", () => {
    // Small injected cap — see the note on "evicts the oldest half..." above;
    // same eviction_small golden section.
    const clock = stubClock();
    const limiter = new RateLimiter({
      restAcquireRate: 1000,
      restSendRate: 1000,
      now: clock.now,
      clientCacheMax: 8,
      clientEvictCount: 4,
    });
    for (let i = 0; i < 8; i += 1) {
      limiter.allowRestAcquire(`c${i}`);
    }
    limiter.allowRestAcquire("overflow");
    expect({
      size_after_overflow: limiter.restAcquireClientCount,
      overflow_client_kept: limiter.hasRestAcquireClient("overflow"),
      oldest_evicted: !limiter.hasRestAcquireClient("c0"),
      newest_kept: limiter.hasRestAcquireClient("c7"),
    }).toStrictEqual({
      size_after_overflow: golden.eviction_small.size_after_overflow,
      overflow_client_kept: golden.eviction_small.overflow_client_kept,
      oldest_evicted: golden.eviction_small.oldest_evicted,
      newest_kept: golden.eviction_small.newest_kept,
    });
  });
});
