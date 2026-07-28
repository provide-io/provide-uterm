//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Single-session hijack arbitration.
 *
 * Port of the Python module `provide.uterm.bridge.coordinator`.
 *
 * The lease state machine — acquire, heartbeat, release — for exactly one
 * session. The hub wraps it with multi-worker management; the Cloudflare
 * Durable Object uses it directly as a single-writer coordinator, which is
 * why it takes no locks and holds no I/O.
 */

import type { HijackSession } from "../hub/index.ts";

/** Shortest lease the coordinator grants. */
export const COORDINATOR_LEASE_MIN_SECONDS = 1;

/**
 * Longest lease the coordinator grants — one hour.
 *
 * Its own bound, not the hub's: the hub caps dashboard leases at ten minutes
 * and REST leases at four hours. Three limits for three different exposures.
 */
export const COORDINATOR_LEASE_MAX_SECONDS = 3600;

/** The outcome of an acquire, heartbeat or release. */
export interface AcquireResult {
  /** Whether the operation succeeded. */
  ok: boolean;
  /** The lease afterwards, when there is one. */
  session?: HijackSession | undefined;
  /** Why it was refused. Callers surface these. */
  error?: string | undefined;
  /** Whether an acquire renewed the caller's own existing lease. */
  isRenewal: boolean;
}

/** Options for {@link HijackCoordinator}. */
export interface HijackCoordinatorOptions {
  /** Monotonic clock in seconds. */
  now?: () => number;
  /** Hijack-id source; injected so a test can pin it. */
  newId?: () => string;
}

/** Options for a heartbeat. */
export interface HeartbeatOptions {
  /** Verified against the lease holder when given. */
  owner?: string;
  /** Monotonic time to measure from. */
  now?: number;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** Clamp a requested lease into the accepted range. */
function clampLease(seconds: number): number {
  return Math.max(COORDINATOR_LEASE_MIN_SECONDS, Math.min(Math.trunc(seconds), COORDINATOR_LEASE_MAX_SECONDS));
}

/** Single-session hijack arbitration. */
export class HijackCoordinator {
  readonly #now: () => number;
  readonly #newId: () => string;
  #session: HijackSession | undefined;

  constructor(options: HijackCoordinatorOptions = {}) {
    this.#now = options.now ?? monotonicNow;
    this.#newId = options.newId ?? (() => crypto.randomUUID());
  }

  /** The live lease, if one is held. */
  get session(): HijackSession | undefined {
    return this.#active(this.#now());
  }

  /**
   * Take the session for `owner`.
   *
   * The same owner asking again renews, and still gets a *fresh* hijack id —
   * so the caller always holds an authoritative token for the current period,
   * and an id captured earlier stops working. That is what makes a leaked
   * token time-bounded rather than permanent.
   *
   * A different owner is refused while the lease is live, and admitted once
   * it lapses: time-bounded expiry is the safety property, so an operator who
   * walks away cannot hold the session forever.
   */
  acquire(owner: string, leaseSeconds: number, now?: number): AcquireResult {
    const at = now ?? this.#now();
    const active = this.#active(at);
    if (active !== undefined && active.owner !== owner) {
      return { ok: false, session: active, error: "already_hijacked", isRenewal: false };
    }
    const isRenewal = active !== undefined;
    this.#session = {
      hijackId: this.#newId(),
      owner,
      leaseExpiresAt: at + clampLease(leaseSeconds),
      acquiredAt: at,
      lastHeartbeat: at,
    };
    return { ok: true, session: this.#session, isRenewal };
  }

  /**
   * Extend the lease.
   *
   * The id is required and the owner is checked when supplied — defence in
   * depth, so a leaked token still cannot be renewed by someone claiming to
   * be a different operator. A heartbeat arriving after the lease lapsed
   * finds nothing rather than resurrecting it.
   */
  heartbeat(hijackId: string, leaseSeconds: number, options: HeartbeatOptions = {}): AcquireResult {
    const at = options.now ?? this.#now();
    const active = this.#active(at);
    if (active === undefined) {
      return { ok: false, session: undefined, error: "not_hijacked", isRenewal: false };
    }
    if (active.hijackId !== hijackId) {
      return { ok: false, session: active, error: "hijack_id_mismatch", isRenewal: false };
    }
    if (options.owner !== undefined && active.owner !== options.owner) {
      return { ok: false, session: active, error: "owner_mismatch", isRenewal: false };
    }
    active.leaseExpiresAt = at + clampLease(leaseSeconds);
    active.lastHeartbeat = at;
    return { ok: true, session: active, isRenewal: false };
  }

  /**
   * Give the session up.
   *
   * Deliberately does not consult expiry — whoever holds the id can always
   * clean up, even if they noticed late.
   */
  release(hijackId: string): AcquireResult {
    const active = this.#session;
    if (active === undefined) {
      return { ok: false, session: undefined, error: "not_hijacked", isRenewal: false };
    }
    if (active.hijackId !== hijackId) {
      return { ok: false, session: active, error: "hijack_id_mismatch", isRenewal: false };
    }
    this.#session = undefined;
    return { ok: true, session: undefined, isRenewal: false };
  }

  /**
   * Whether the holder of `hijackId` may type right now.
   *
   * Closes on time rather than on release: an operator who stops
   * heartbeating stops being able to send.
   */
  canSendInput(hijackId?: string): boolean {
    const active = this.session;
    return active !== undefined && hijackId === active.hijackId;
  }

  /**
   * The lease if it is still live, clearing it if it is not.
   *
   * The sweep happens on read so a later acquire never has to know the
   * previous holder existed.
   */
  #active(at: number): HijackSession | undefined {
    const session = this.#session;
    if (session === undefined) {
      return undefined;
    }
    if (session.leaseExpiresAt <= at) {
      this.#session = undefined;
      return undefined;
    }
    return session;
  }
}
