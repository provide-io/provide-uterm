//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Token-bucket rate limiting for the hub's REST endpoints.
 *
 * {@link TokenBucket} is the primitive; {@link RateLimiter} composes named
 * policies from it — a global bucket and a per-client bucket per purpose,
 * both of which must allow.
 *
 * Port of the Python modules `provide.uterm.server.bridge.ratelimit` and
 * `...bridge.hub.limiter`, and the Go package `hub`.
 *
 * No locking. Each `allow` is a short computation on private state, and the
 * per-client map is only ever mutated from the same call, so concurrent
 * callers cannot interleave inside one decision.
 */

/**
 * Maximum per-client buckets held at once.
 *
 * On overflow the oldest half are evicted, which bounds memory while keeping
 * rate-limit state for recently-active clients.
 */
export const REST_CLIENT_CACHE_MAX = 1024;
/** How many entries an overflow evicts, so the next one is amortised. */
export const REST_CLIENT_EVICT_COUNT = REST_CLIENT_CACHE_MAX / 2;

/**
 * Tightest rate (tokens/sec) any bucket-backed policy may be configured with.
 *
 * This is 1.0 for a structural reason, not a taste one: {@link TokenBucket}
 * defaults its burst to one second of the rate, so a bucket configured below
 * 1.0 can never hold a whole token and therefore denies *every* call forever,
 * however long the caller waits. A rate in `[0, 1)` is a bricked endpoint
 * wearing the costume of a rate limit, so the server configuration refuses the
 * whole band rather than accepting a number that silently means "never".
 *
 * {@link RateLimiter} also clamps to this floor. Config refusing below it
 * keeps the clamp from quietly handing back a *looser* limit than the operator
 * wrote.
 *
 * Making sub-1 rates meaningful would mean decoupling burst from rate
 * (`burst = max(1.0, rate)`) — a change to token-bucket semantics across every
 * port and their recorded goldens. Worth doing deliberately if a sub-1 policy
 * is ever actually wanted; not worth doing by accident here.
 */
export const MIN_RATE_PER_SEC = 1.0;

/** Construction options for {@link TokenBucket}. */
export interface TokenBucketOptions {
  /** Maximum burst. Defaults to one second of capacity at the rate. */
  burst?: number;
  /** Monotonic clock in seconds. Injected so tests need not sleep. */
  now?: () => number;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** A token bucket: `rate` tokens per second, capped at `burst`. */
export class TokenBucket {
  readonly #rate: number;
  readonly #burst: number;
  readonly #now: () => number;
  #tokens: number;
  #lastRefill: number;

  constructor(rate: number, options: TokenBucketOptions = {}) {
    this.#rate = rate;
    this.#burst = options.burst ?? rate;
    this.#now = options.now ?? monotonicNow;
    this.#tokens = this.#burst;
    this.#lastRefill = this.#now();
  }

  /** Tokens currently available, for inspection. */
  get tokens(): number {
    return this.#tokens;
  }

  /**
   * Consume one token if available.
   *
   * Refills for the elapsed time first, capped at the burst, so a long idle
   * period grants a full bucket rather than an unbounded credit.
   */
  allow(): boolean {
    const now = this.#now();
    const elapsed = now - this.#lastRefill;
    this.#tokens = Math.min(this.#burst, this.#tokens + elapsed * this.#rate);
    this.#lastRefill = now;
    if (this.#tokens >= 1) {
      this.#tokens -= 1;
      return true;
    }
    return false;
  }
}

/** Construction options for {@link RateLimiter}. */
export interface RateLimiterOptions {
  /** Tokens per second for the REST acquire policy. */
  restAcquireRate: number;
  /** Tokens per second for the REST send policy. */
  restSendRate: number;
  /** Monotonic clock shared by every bucket this limiter mints. */
  now?: () => number;
  /**
   * Per-client cache cap and eviction batch size. Default to the real
   * {@link REST_CLIENT_CACHE_MAX} / {@link REST_CLIENT_EVICT_COUNT} constants —
   * this exists so a test can exercise the LRU-eviction algorithm at a scale
   * that does not risk a mutation-testing tool's hit-count safety valve (see
   * {@link evictIfFull}'s docstring), not for production tuning.
   */
  clientCacheMax?: number;
  clientEvictCount?: number;
}

/** Composes per-purpose token buckets for the hub's REST endpoints. */
export class RateLimiter {
  /** Global REST acquire bucket. Replaceable, which the hub's tests rely on. */
  restAcquireBucket: TokenBucket;
  /** Global REST send bucket. */
  restSendBucket: TokenBucket;

  readonly #acquireRate: number;
  readonly #sendRate: number;
  readonly #now: (() => number) | undefined;
  readonly #cacheMax: number;
  readonly #evictCount: number;
  /** Insertion order is recency order; see {@link #touch}. */
  readonly #acquirePerClient = new Map<string, TokenBucket>();
  readonly #sendPerClient = new Map<string, TokenBucket>();

  constructor(options: RateLimiterOptions) {
    // Any rate under the floor would deny every request forever — see
    // {@link MIN_RATE_PER_SEC} — which is a misconfiguration rather than a
    // policy; floor it instead.
    this.#acquireRate = Math.max(MIN_RATE_PER_SEC, options.restAcquireRate);
    this.#sendRate = Math.max(MIN_RATE_PER_SEC, options.restSendRate);
    this.#now = options.now;
    this.#cacheMax = options.clientCacheMax ?? REST_CLIENT_CACHE_MAX;
    this.#evictCount = options.clientEvictCount ?? REST_CLIENT_EVICT_COUNT;
    this.restAcquireBucket = this.#mint(this.#acquireRate);
    this.restSendBucket = this.#mint(this.#sendRate);
  }

  /** Configured tokens per second for the REST acquire policy. */
  get restAcquireRate(): number {
    return this.#acquireRate;
  }

  /** Configured tokens per second for the REST send policy. */
  get restSendRate(): number {
    return this.#sendRate;
  }

  /** How many per-client acquire buckets are held, for inspection. */
  get restAcquireClientCount(): number {
    return this.#acquirePerClient.size;
  }

  /** Whether a per-client acquire bucket is held, for inspection. */
  hasRestAcquireClient(clientId: string): boolean {
    return this.#acquirePerClient.has(clientId);
  }

  /** Build a bucket on this limiter's clock. */
  #mint(rate: number): TokenBucket {
    return new TokenBucket(rate, this.#now === undefined ? {} : { now: this.#now });
  }

  /**
   * Whether `clientId` passes both the acquire limits.
   *
   * The per-client bucket is consumed first and short-circuits: a client
   * hammering its own limit must not drain the shared budget for everyone
   * else.
   */
  allowRestAcquire(clientId: string): boolean {
    const bucket = this.#touch(this.#acquirePerClient, clientId, this.#acquireRate);
    return bucket.allow() && this.restAcquireBucket.allow();
  }

  /** Whether `clientId` passes both the send limits. */
  allowRestSend(clientId: string): boolean {
    const bucket = this.#touch(this.#sendPerClient, clientId, this.#sendRate);
    return bucket.allow() && this.restSendBucket.allow();
  }

  /**
   * Get or create a client's bucket, mark it most-recently-used, then evict.
   *
   * A `Map` preserves insertion order, so deleting and reinserting an
   * existing key moves it to the recent end while keeping its bucket
   * *state*. Eviction runs last and therefore never drops the key just
   * touched — which is the point: if a drained bucket could be evicted and
   * recreated full, a client could reset its own limit by flooding the cache
   * with new identifiers.
   */
  #touch(perClient: Map<string, TokenBucket>, clientId: string, rate: number): TokenBucket {
    const existing = perClient.get(clientId);
    perClient.delete(clientId);
    const bucket = existing ?? this.#mint(rate);
    perClient.set(clientId, bucket);
    evictIfFull(perClient, this.#cacheMax, this.#evictCount);
    return bucket;
  }
}

/**
 * Drop the oldest entries once the cache is over its cap.
 *
 * Insertion order is recency order, so trimming from the front is a true LRU
 * eviction. The client that triggered this has already been reinserted at the
 * end and is outside the window.
 *
 * `cap`/`evictCount` default to the real production constants; tests that
 * only need to prove the *algorithm* (not the specific production numbers)
 * pass small ones instead. That is not a testing nicety: removing the `break`
 * below turns an early exit — hit once per overflow in real use — into a full
 * rescan of whatever the map's actual size is. At the real cap (1024) that is
 * enough of a blowup to trip a mutation-testing tool's runaway-loop safety net
 * before any assertion runs, on a mutant that is a completely ordinary, real
 * bug. Exercising the same code at a small scale keeps the mutant a normal,
 * cleanly-killable one instead of a resource-limit crash.
 *
 * The two golden-bound tests in ratelimit.test.ts (`"evicts the oldest half
 * once the cap is passed"`, `"matches the recorded eviction outcome"`) run
 * at `clientCacheMax:8`/`clientEvictCount:4` against `ratelimit_golden.json`'s
 * `eviction_small` section — generated from the same Python reference
 * algorithm as the real-scale `eviction` section (see
 * `gen_ratelimit_golden.py`'s `_eviction_small_record`), just at a scale small
 * enough that removing this `break` stays a clean Stryker `Killed` rather
 * than tripping the hardcoded `HIT_LIMIT_FACTOR = 100` hit-count safety valve
 * (confirmed: at the real 1024/512 scale this same mutant's blast radius
 * exceeded that budget before either test's assertion ran, reporting
 * `Timeout` instead of `Killed`). `"exposes the documented cache bounds"`
 * still pins the real 1024/512 production constants directly, with no loop.
 */
function evictIfFull(
  perClient: Map<string, TokenBucket>,
  cap: number = REST_CLIENT_CACHE_MAX,
  evictCount: number = REST_CLIENT_EVICT_COUNT,
): void {
  if (perClient.size <= cap) {
    return;
  }
  let removed = 0;
  for (const key of perClient.keys()) {
    if (removed >= evictCount) {
      break;
    }
    perClient.delete(key);
    removed += 1;
  }
}
