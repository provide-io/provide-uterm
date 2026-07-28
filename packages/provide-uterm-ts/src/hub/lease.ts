//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The hijack lease state machine: who is allowed to drive a worker.
 *
 * Port of the Python module `provide.uterm.server.bridge.hub.lease` and the
 * Go package `hub` (`lease*.go`).
 *
 * Ownership arrives by two paths — a dashboard WebSocket lease and a REST
 * session lease — and only one may be active at a time. This arbitrates
 * between them, expires what has lapsed, and tells the hub when a worker has
 * become free again.
 *
 * **On locking.** The reference holds the hub's `asyncio.Lock` across each
 * check-then-set. Nothing here does, and that is not a shortcut: every
 * critical section below runs to completion without an `await`, so no other
 * task can observe a half-applied transition. The one section that genuinely
 * spans an await is the REST acquire's worker-pause write, and that is
 * exactly why the reference reserves the slot with `hijackPending` before
 * writing — the reservation, not the lock, is what keeps the acquire
 * mutually exclusive. That mechanism is reproduced faithfully.
 *
 * Every cross-cutting effect (broadcast, event append, worker send, prune,
 * metrics) is injected rather than imported, so this module never reaches
 * back into the hub's module graph.
 */

import { encodeWorkerFrame } from "./frames.ts";
import type { Connection, HijackSession, WorkerTermState } from "./models.ts";
import type { WorkerRegistry } from "./registry.ts";

/** Shortest dashboard lease the hub will grant, in seconds. */
export const DASHBOARD_LEASE_MIN_SECONDS = 1;

/**
 * Longest dashboard lease the hub will grant, in seconds — ten minutes.
 *
 * Shorter than the REST cap on purpose: a browser that goes away stops
 * heartbeating, and a long dashboard lease would strand the worker until it
 * lapsed.
 */
export const DASHBOARD_LEASE_MAX_SECONDS = 600;

/** The hub surface the lease manager calls back into. */
export interface LeaseHubCallbacks {
  /** Whether any hijack is active on `state`. */
  isHijacked(state: WorkerTermState): boolean;
  /** Whether a dashboard hijack is active on `state`. */
  isDashboardHijackActive(state: WorkerTermState): boolean;
  /** Whether an unexpired REST lease is held on `state`. */
  hasValidRestLease(state: WorkerTermState): boolean;
  /** Whether `ws` may send input to `state`. */
  canSendInput(state: WorkerTermState, ws: Connection): boolean;
  /** Emit a named metric. */
  metric(name: string): void;
  /** Tell subscribers a worker's hijack state changed. */
  notifyHijackChanged(workerId: string, enabled: boolean, owner?: string): void;
  /** Send a message to the worker. */
  sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean>;
  /** Push the current hijack state to attached browsers. */
  broadcastHijackState(workerId: string): Promise<void>;
  /** Append an event to the worker's log. */
  appendEvent(workerId: string, eventType: string): Promise<Record<string, unknown>>;
  /** Drop worker state that no longer has connections or leases. */
  pruneIfIdle(workerId: string): Promise<void>;
  /** Re-check for a concurrent acquire, then resume the worker if still free. */
  recheckAndResume(workerId: string, now: number): Promise<void>;
}

/** Construction options for {@link HijackLeaseManager}. */
export interface HijackLeaseManagerOptions {
  /** The worker table this manager arbitrates over. */
  registry: WorkerRegistry<WorkerTermState>;
  /** Cross-cutting effects, injected to avoid importing the hub. */
  hub: LeaseHubCallbacks;
  /** TTL for dashboard leases, clamped into the accepted range. */
  dashboardLeaseSeconds: number;
  /** Monotonic clock in seconds, for lease arithmetic. */
  now?: () => number;
  /** Wall clock in seconds, for timestamps sent to the worker. */
  wallNow?: () => number;
}

/** Arguments for a REST hijack acquire. */
export interface AcquireRestOptions {
  /** Self-declared label for whoever is acquiring. */
  owner: string;
  /** Requested lease duration. */
  leaseSeconds: number;
  /** Identifier the heartbeat and release calls will quote. */
  hijackId: string;
  /** Monotonic time the lease is measured from. */
  now: number;
}

/** The outcome of an acquire attempt. */
export interface AcquireResult {
  /** Whether the lease was granted. */
  ok: boolean;
  /** Why it was refused; surfaced by the API, so the exact string matters. */
  reason?: string;
}

/** Which halves of a lease have lapsed. */
export interface LeaseExpirations {
  /** The dashboard lease has lapsed. */
  browserExpired: boolean;
  /** The REST lease has lapsed. */
  restExpired: boolean;
}

/** The events window returned to a REST events poll. */
export interface EventsData {
  /** The events in the requested window. */
  rows: Array<Record<string, unknown>>;
  /** Sequence number of the newest event the worker has. */
  latestSeq: number;
  /** Sequence number of the oldest event still retained. */
  minEventSeq: number;
  /** Current lease expiry, so the caller can renew without a second call. */
  freshExpires: number;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** Clamp a dashboard lease TTL, truncating toward zero as the reference does. */
function clampDashboardLease(seconds: number): number {
  return Math.max(DASHBOARD_LEASE_MIN_SECONDS, Math.min(Math.trunc(seconds), DASHBOARD_LEASE_MAX_SECONDS));
}

/** Multi-worker hijack lease state machine. */
export class HijackLeaseManager {
  readonly #registry: WorkerRegistry<WorkerTermState>;
  readonly #hub: LeaseHubCallbacks;
  readonly #now: () => number;
  readonly #wallNow: () => number;
  #dashboardLeaseSeconds: number;

  constructor(options: HijackLeaseManagerOptions) {
    this.#registry = options.registry;
    this.#hub = options.hub;
    this.#now = options.now ?? monotonicNow;
    this.#wallNow = options.wallNow ?? (() => Date.now() / 1000);
    this.#dashboardLeaseSeconds = clampDashboardLease(options.dashboardLeaseSeconds);
  }

  /**
   * Which halves of `state`'s lease have lapsed at `now`.
   *
   * A read, not a sweep: it reports what *would* expire without clearing
   * anything, so a caller can decide whether acting is worth the write.
   */
  static computeLeaseExpirations(state: WorkerTermState, now: number): LeaseExpirations {
    const lease = state.lease;
    return {
      browserExpired: lease.ws !== undefined && lease.wsExpiresAt !== undefined && lease.wsExpiresAt <= now,
      restExpired: lease.session !== undefined && lease.session.leaseExpiresAt <= now,
    };
  }

  /** Configured dashboard lease TTL, in seconds. */
  get dashboardLeaseSeconds(): number {
    return this.#dashboardLeaseSeconds;
  }

  set dashboardLeaseSeconds(value: number) {
    this.#dashboardLeaseSeconds = clampDashboardLease(value);
  }

  /**
   * Reserve a REST hijack, pause the worker, then finalise the lease.
   *
   * The pause write runs *outside* any critical section on purpose: a single
   * backpressured worker holding the hub's lock across a socket write would
   * stall every other operation. The slot is reserved first with
   * `hijackPending` so a concurrent acquire — from either path — sees it as
   * taken during that window, and the reservation is rolled back if the write
   * fails or the worker vanishes.
   */
  async tryAcquireRest(workerId: string, options: AcquireRestOptions): Promise<AcquireResult> {
    // Phase 1 — reserve. No awaits, so this is atomic against other tasks.
    const state = this.#registry.get(workerId);
    if (state === undefined || state.workerWs === undefined) {
      return { ok: false, reason: "no_worker" };
    }
    if (state.inputMode === "open") {
      return { ok: false, reason: "open_mode" };
    }
    if (
      this.#hub.isDashboardHijackActive(state) ||
      this.#hub.hasValidRestLease(state) ||
      state.hijackPending !== undefined
    ) {
      return { ok: false, reason: "already_hijacked" };
    }
    const workerWs = state.workerWs;
    state.hijackPending = options.hijackId;

    try {
      // Phase 2 — pause the worker, outside the reservation's critical section.
      try {
        await workerWs.sendText(
          encodeWorkerFrame({
            type: "control",
            action: "pause",
            owner: options.owner,
            hijack_id: options.hijackId,
            ts: this.#wallNow(),
          }),
        );
      } catch {
        // A worker that cannot be paused is gone. Clearing the socket stops
        // the next acquire believing there is still a worker there.
        const current = this.#registry.get(workerId);
        if (current !== undefined && current.workerWs === workerWs) {
          current.workerWs = undefined;
        }
        return { ok: false, reason: "no_worker" };
      }

      // Phase 3 — finalise, unless something superseded this reservation.
      const current = this.#registry.get(workerId);
      if (current === undefined || current.hijackPending !== options.hijackId) {
        return { ok: false, reason: "no_worker" };
      }
      current.hijackSession = {
        hijackId: options.hijackId,
        owner: options.owner,
        acquiredAt: options.now,
        leaseExpiresAt: options.now + options.leaseSeconds,
        lastHeartbeat: options.now,
      };
      current.hijackPending = undefined;
    } finally {
      // Roll back a reservation that is still this acquire's own. On success
      // phase 3 already cleared it; a competing acquire's reservation carries
      // a different id and is left alone.
      const current = this.#registry.get(workerId);
      if (current !== undefined && current.hijackPending === options.hijackId) {
        current.hijackPending = undefined;
      }
    }
    return { ok: true };
  }

  /**
   * Take the dashboard hijack for `ws`.
   *
   * An outstanding REST reservation counts as taken: the pause window is
   * precisely when dual ownership would otherwise be possible. Unlike the
   * REST path, open input mode is *not* a guard here.
   */
  async tryAcquireWs(workerId: string, ws: Connection): Promise<AcquireResult> {
    const state = this.#registry.get(workerId);
    if (state === undefined || state.workerWs === undefined) {
      return { ok: false, reason: "no_worker" };
    }
    if (
      this.#hub.isDashboardHijackActive(state) ||
      this.#hub.hasValidRestLease(state) ||
      state.hijackPending !== undefined
    ) {
      return { ok: false, reason: "already_hijacked" };
    }
    state.hijackOwner = ws;
    state.hijackOwnerExpiresAt = this.#now() + this.#dashboardLeaseSeconds;
    return { ok: true };
  }

  /** Extend whoever holds the dashboard lease; returns the new expiry. */
  async touchOwner(workerId: string, leaseSeconds?: number): Promise<number | undefined> {
    const state = this.#registry.get(workerId);
    if (state === undefined || state.hijackOwner === undefined) {
      return undefined;
    }
    const ttl = leaseSeconds === undefined ? this.#dashboardLeaseSeconds : clampDashboardLease(leaseSeconds);
    state.hijackOwnerExpiresAt = this.#now() + ttl;
    return state.hijackOwnerExpiresAt;
  }

  /**
   * Extend the dashboard lease, but only for the browser that holds it.
   *
   * Without the identity check any connected browser could keep someone
   * else's hold alive indefinitely.
   */
  async touchIfOwner(workerId: string, ws: Connection): Promise<number | undefined> {
    const state = this.#registry.get(workerId);
    if (state === undefined || !this.#hub.isDashboardHijackActive(state) || state.hijackOwner !== ws) {
      return undefined;
    }
    state.hijackOwnerExpiresAt = this.#now() + this.#dashboardLeaseSeconds;
    return state.hijackOwnerExpiresAt;
  }

  /**
   * Release the dashboard lease held by `ws`.
   *
   * Reports whether a REST lease is still live either way, because the caller
   * uses that to decide whether the worker may resume.
   */
  async tryReleaseWs(workerId: string, ws: Connection): Promise<{ ok: boolean; restActive: boolean }> {
    const state = this.#registry.get(workerId);
    if (state === undefined || !this.#hub.isDashboardHijackActive(state) || state.hijackOwner !== ws) {
      return { ok: false, restActive: state !== undefined && this.#hub.hasValidRestLease(state) };
    }
    state.hijackOwner = undefined;
    state.hijackOwnerExpiresAt = undefined;
    return { ok: true, restActive: this.#hub.hasValidRestLease(state) };
  }

  /** Clear the REST lease, reporting whether the worker may now resume. */
  async releaseRest(workerId: string, hijackId: string): Promise<{ ok: boolean; shouldResume: boolean }> {
    const state = this.#registry.get(workerId);
    if (state === undefined || state.hijackSession === undefined || state.hijackSession.hijackId !== hijackId) {
      return { ok: false, shouldResume: false };
    }
    state.hijackSession = undefined;
    // A dashboard lease may still be held; resuming then would hand the
    // worker back while a browser is still driving it.
    return { ok: true, shouldResume: !this.#hub.isDashboardHijackActive(state) };
  }

  /**
   * Extend the REST lease on a heartbeat.
   *
   * Verifies the owner as well as the hijack id: knowing a leaked id must not
   * be enough to keep someone else's lease alive.
   */
  async extendLease(
    workerId: string,
    hijackId: string,
    owner: string,
    leaseSeconds: number,
    now: number,
  ): Promise<number | undefined> {
    const state = this.#registry.get(workerId);
    const session = state?.hijackSession;
    if (session === undefined || session.hijackId !== hijackId) {
      return undefined;
    }
    if (session.owner !== owner) {
      this.#hub.metric("hijack_heartbeat_denied_owner_mismatch");
      return undefined;
    }
    session.lastHeartbeat = now;
    session.leaseExpiresAt = now + leaseSeconds;
    return session.leaseExpiresAt;
  }

  /** Re-read the current lease expiry, falling back when the id has moved on. */
  async getFreshExpiry(workerId: string, hijackId: string, fallback: number): Promise<number> {
    const session = this.#registry.get(workerId)?.hijackSession;
    if (session !== undefined && session.hijackId === hijackId) {
      return session.leaseExpiresAt;
    }
    return fallback;
  }

  /** Whether the REST lease for `hijackId` is present and unexpired. */
  async checkValid(workerId: string, hijackId: string): Promise<boolean> {
    const session = this.#registry.get(workerId)?.hijackSession;
    return session !== undefined && session.hijackId === hijackId && session.leaseExpiresAt > this.#now();
  }

  /**
   * The live REST session for `hijackId`.
   *
   * Sweeps expired leases first, so a caller cannot act on one that has
   * lapsed but not yet been cleaned up.
   */
  async getRestSession(workerId: string, hijackId: string): Promise<HijackSession | undefined> {
    await this.cleanupExpired(workerId);
    const session = this.#registry.get(workerId)?.hijackSession;
    if (session === undefined || session.leaseExpiresAt <= this.#now() || session.hijackId !== hijackId) {
      return undefined;
    }
    return session;
  }

  /**
   * Expire lapsed leases, resume the worker if that left it free, and tell
   * everyone what happened.
   *
   * The order is the contract: the resume re-check runs before the events are
   * appended and before the state is broadcast.
   */
  async cleanupExpired(workerId: string): Promise<boolean> {
    const now = this.#now();
    const state = this.#registry.get(workerId);
    if (state === undefined) {
      return false;
    }
    const lease = state.lease;
    if (lease.isIdle) {
      return false;
    }
    const { restExpired, dashExpired } = lease.expire(now);
    if (!restExpired && !dashExpired) {
      return false;
    }
    state.applyLease(lease);
    const shouldResume = lease.isIdle;

    this.#hub.metric("hijack_lease_expiries_total");
    if (shouldResume) {
      await this.#hub.recheckAndResume(workerId, now);
    }
    if (restExpired) {
      await this.#hub.appendEvent(workerId, "hijack_lease_expired");
    }
    if (dashExpired) {
      await this.#hub.appendEvent(workerId, "hijack_owner_expired");
    }
    await this.#hub.broadcastHijackState(workerId);
    await this.#hub.pruneIfIdle(workerId);
    return true;
  }

  /**
   * Resume the worker, unless someone acquired it in the meantime.
   *
   * The gap between the sweep and this call is real — another client can take
   * the lease in it — and resuming then would drop the new holder's input.
   */
  async recheckAndResume(workerId: string, now: number): Promise<void> {
    const state = this.#registry.get(workerId);
    if (state !== undefined && this.#hub.isHijacked(state)) {
      return;
    }
    await this.#hub.sendWorker(workerId, {
      type: "control",
      action: "resume",
      owner: "lease-expired",
      lease_s: 0,
      ts: now,
    });
    this.#hub.notifyHijackChanged(workerId, false);
  }

  /**
   * Drop dead browser sockets, resuming the worker if the holder was one.
   *
   * Nobody is left to release a lease whose owner's socket has closed, so
   * without this the worker would stay paused until the lease lapsed.
   */
  async removeDeadBrowsers(workerId: string, dead: Set<Connection>): Promise<boolean> {
    let notifyHijackOff = false;
    const state = this.#registry.get(workerId);
    if (state !== undefined) {
      for (const ws of dead) {
        state.browsers.delete(ws);
        if (this.#hub.isDashboardHijackActive(state) && state.hijackOwner === ws) {
          state.hijackOwner = undefined;
          state.hijackOwnerExpiresAt = undefined;
          // Subsumed by the isHijacked re-check below, which reaches the same
          // conclusion for a live REST lease. Kept because the reference has
          // it, not because any outcome depends on it.
          notifyHijackOff = !this.#hub.hasValidRestLease(state);
        }
      }
    }
    if (notifyHijackOff) {
      // Re-check: a concurrent acquire may have taken the worker between
      // clearing the owner above and the send below.
      const current = this.#registry.get(workerId);
      if (current !== undefined && this.#hub.isHijacked(current)) {
        notifyHijackOff = false;
      }
    }
    if (notifyHijackOff) {
      await this.#hub.sendWorker(workerId, {
        type: "control",
        action: "resume",
        owner: "dead-socket",
        lease_s: 0,
        ts: this.#wallNow(),
      });
      this.#hub.notifyHijackChanged(workerId, false);
    }
    return notifyHijackOff;
  }

  /** Whether any hijack is currently active on `workerId`. */
  async stillHijacked(workerId: string): Promise<boolean> {
    const state = this.#registry.get(workerId);
    return state !== undefined && this.#hub.isHijacked(state);
  }

  /** Whether `workerId` is accepting input from any operator. */
  async isInputOpenMode(workerId: string): Promise<boolean> {
    return this.#registry.get(workerId)?.inputMode === "open";
  }

  /**
   * Whether `ws` may send input, extending its lease if it is the holder.
   *
   * The extension rides on the input itself, so an operator who is actively
   * typing never has to heartbeat separately.
   */
  async prepareBrowserInput(workerId: string, ws: Connection): Promise<boolean> {
    const state = this.#registry.get(workerId);
    if (state === undefined) {
      return false;
    }
    const allowed = this.#hub.canSendInput(state, ws);
    if (this.#hub.isDashboardHijackActive(state) && state.hijackOwner === ws) {
      state.hijackOwnerExpiresAt = this.#now() + this.#dashboardLeaseSeconds;
    }
    return allowed;
  }

  /**
   * The events a REST poller has not seen yet.
   *
   * Filtering by sequence happens before the limit is applied: limiting first
   * would return rows the caller already has and silently drop the ones it
   * does not.
   */
  async getEventsData(
    workerId: string,
    hijackId: string,
    fallback: HijackSession,
    afterSeq: number,
    limit: number,
  ): Promise<EventsData> {
    const state = this.#registry.get(workerId);
    if (state === undefined) {
      return { rows: [], latestSeq: 0, minEventSeq: 0, freshExpires: fallback.leaseExpiresAt };
    }
    const rows = state.events
      .toArray()
      .filter((event) => Number(event.seq ?? 0) > afterSeq)
      .slice(0, limit);
    const session = state.hijackSession;
    return {
      rows,
      latestSeq: state.eventSeq,
      minEventSeq: state.minEventSeq,
      freshExpires:
        session !== undefined && session.hijackId === hijackId ? session.leaseExpiresAt : fallback.leaseExpiresAt,
    };
  }
}
