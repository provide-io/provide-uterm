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
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1000, restSendRate: 1000, now: clock.now });
    for (let i = 0; i < REST_CLIENT_CACHE_MAX; i += 1) {
      limiter.allowRestAcquire(`c${i}`);
    }
    expect(limiter.restAcquireClientCount).toBe(golden.eviction.size_at_cap);
    limiter.allowRestAcquire("overflow");
    expect(limiter.restAcquireClientCount).toBe(golden.eviction.size_after_overflow);
  });

  it("never evicts the client it is currently serving", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1000, restSendRate: 1000, now: clock.now });
    for (let i = 0; i < REST_CLIENT_CACHE_MAX; i += 1) {
      limiter.allowRestAcquire(`c${i}`);
    }
    limiter.allowRestAcquire("overflow");
    expect(limiter.hasRestAcquireClient("overflow")).toBe(true);
    expect(limiter.hasRestAcquireClient("c0")).toBe(false);
    expect(limiter.hasRestAcquireClient(`c${REST_CLIENT_CACHE_MAX - 1}`)).toBe(true);
  });

  it("refreshes recency on access rather than only on insert", () => {
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1000, restSendRate: 1000, now: clock.now });
    limiter.allowRestAcquire("early");
    for (let i = 0; i < REST_CLIENT_CACHE_MAX - 1; i += 1) {
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
    // A zero rate would deny every request forever, which is a
    // configuration mistake rather than a policy anyone wants.
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 0, restSendRate: -5, now: clock.now });
    expect({ acquire: limiter.restAcquireRate, send: limiter.restSendRate }).toStrictEqual({
      acquire: 0.1,
      send: 0.1,
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
    const clock = stubClock();
    const limiter = new RateLimiter({ restAcquireRate: 1000, restSendRate: 1000, now: clock.now });
    for (let i = 0; i < REST_CLIENT_CACHE_MAX; i += 1) {
      limiter.allowRestAcquire(`c${i}`);
    }
    limiter.allowRestAcquire("overflow");
    expect({
      size_after_overflow: limiter.restAcquireClientCount,
      overflow_client_kept: limiter.hasRestAcquireClient("overflow"),
      oldest_evicted: !limiter.hasRestAcquireClient("c0"),
      newest_kept: limiter.hasRestAcquireClient(`c${REST_CLIENT_CACHE_MAX - 1}`),
    }).toStrictEqual({
      size_after_overflow: golden.eviction.size_after_overflow,
      overflow_client_kept: golden.eviction.overflow_client_kept,
      oldest_evicted: golden.eviction.oldest_evicted,
      newest_kept: golden.eviction.newest_kept,
    });
  });
});
