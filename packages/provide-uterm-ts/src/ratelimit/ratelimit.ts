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

/** The minimum configurable rate; below this a policy would deny forever. */
const MIN_RATE = 0.1;

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
  /** Insertion order is recency order; see {@link #touch}. */
  readonly #acquirePerClient = new Map<string, TokenBucket>();
  readonly #sendPerClient = new Map<string, TokenBucket>();

  constructor(options: RateLimiterOptions) {
    // A zero or negative rate would deny every request forever, which is a
    // misconfiguration rather than a policy; floor it instead.
    this.#acquireRate = Math.max(MIN_RATE, options.restAcquireRate);
    this.#sendRate = Math.max(MIN_RATE, options.restSendRate);
    this.#now = options.now;
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
    evictIfFull(perClient);
    return bucket;
  }
}

/**
 * Drop the oldest entries once the cache is over its cap.
 *
 * Insertion order is recency order, so trimming from the front is a true LRU
 * eviction. The client that triggered this has already been reinserted at the
 * end and is outside the window.
 */
function evictIfFull(perClient: Map<string, TokenBucket>): void {
  if (perClient.size <= REST_CLIENT_CACHE_MAX) {
    return;
  }
  let removed = 0;
  for (const key of perClient.keys()) {
    if (removed >= REST_CLIENT_EVICT_COUNT) {
      break;
    }
    perClient.delete(key);
    removed += 1;
  }
}
