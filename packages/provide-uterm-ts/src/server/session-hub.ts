//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The hub this server runs: the nine services, composed and wired to each
 * other.
 *
 * Port of `provide.uterm.server.bridge.hub.TermHub` — the composition rather
 * than the services. Every service was already ported and tested on its own in
 * `../hub/`; each one takes the surface it needs back from the hub as injected
 * callbacks, and until something supplied those callbacks there was no hub,
 * only nine pieces of one. This is what supplies them.
 *
 * Three of the reference's methods have no service of their own and are
 * written out here, exactly as the reference keeps them on the composed hub
 * (`router_impl`/`connection`): {@link SessionHub.appendEvent},
 * {@link SessionHub.sendWorker}, {@link SessionHub.setInputMode},
 * {@link SessionHub.updateLastSnapshot} and {@link SessionHub.pruneIfIdle}.
 *
 * ## What is deliberately absent
 *
 * The reference hub also carries the approval store, which is not composed
 * here. The REST rate limiter now is: {@link SessionHub.limiter} is built on
 * this hub's own monotonic clock, so a test that drives the clock drives the
 * window with it rather than waiting one out.
 *
 * Browsers are a different case. This server binds no WebSocket at all, so no
 * browser can attach: `broadcast` and `broadcastHijackState` run their real
 * code over an empty recipient set, and the role a browser would resolve to is
 * the one an unattached connection gets. Nothing is stubbed to make that true;
 * it is true because there is no browser transport to attach through.
 */

import {
  ConnectionManager,
  clampLease,
  encodeWorkerFrame,
  HijackLeaseManager,
  type InputMode,
  MessageRouter,
  PollingCoordinator,
  PresenceManager,
  StateStore,
  WorkerRegistry,
  type WorkerSocket,
  type WorkerTermState,
} from "../hub/index.ts";
import { RateLimiter } from "../ratelimit/index.ts";

/** Longest dashboard lease this hub hands out, in seconds. */
export const SESSION_HUB_DASHBOARD_LEASE_S = 30;

/** How many distinct workers this hub will hold — the reference's bound. */
export const SESSION_HUB_MAX_WORKERS = 10000;

/** Cap on one browser's un-terminated input buffer — the reference's bound. */
export const SESSION_HUB_MAX_BUFFER_CHARS = 40000;

/** Cap on one REST send's keystrokes — the reference's bound. */
export const SESSION_HUB_MAX_INPUT_CHARS = 10000;

/** How many browsers one authenticated principal may hold. */
export const SESSION_HUB_MAX_CONNECTIONS_PER_PRINCIPAL = 25;

/**
 * Acquires per second one address may make — the reference's own default.
 *
 * The configuration key `rest_acquire_rate_limit_per_sec` overrides it, as the
 * reference's does; this is what a deployment that has never heard of the key
 * gets, which is what it got before the key existed.
 */
export const SESSION_HUB_REST_ACQUIRE_RATE = 5;

/**
 * Sends per second one address may make. Steps are charged against it too.
 *
 * Overridden by `rest_send_rate_limit_per_sec`.
 */
export const SESSION_HUB_REST_SEND_RATE = 20;

/** Options for {@link SessionHub}. Defaults are the real clocks. */
export interface SessionHubOptions {
  /** Monotonic seconds, for lease arithmetic. */
  now?: (() => number) | undefined;
  /** Wall seconds, for the timestamps that leave the process. */
  wallNow?: (() => number) | undefined;
  /** How a poll waits. Injected so a test need not spend the time. */
  sleep?: ((seconds: number) => Promise<void>) | undefined;
  /** Acquires per second one address may make. The reference's default if unset. */
  restAcquireRate?: number | undefined;
  /** Sends — and steps — per second one address may make. */
  restSendRate?: number | undefined;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** Sleep for `seconds`, the only way this hub spends time. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, seconds * 1000);
  });
}

/** The hub: workers, leases, snapshots and the events log. */
export class SessionHub {
  /** The worker table every service reads. */
  readonly registry = new WorkerRegistry<WorkerTermState>();
  /** Browsers mid-handshake. Always empty: nothing here accepts a browser. */
  readonly startupPendingBrowsers = new Set<object>();
  readonly store: StateStore;
  readonly lease: HijackLeaseManager;
  readonly router: MessageRouter;
  readonly presence: PresenceManager;
  readonly polling: PollingCoordinator;
  readonly connections: ConnectionManager;
  /**
   * What the lease routes charge a caller's address against.
   *
   * Composed here rather than per route so that a client's budget follows the
   * client and not the endpoint: one address hammering `acquire` on one worker
   * is refused on every other worker too, which is the point of a limit keyed
   * on where the traffic comes from.
   */
  readonly limiter: RateLimiter;
  /** Cap on one REST send's keystrokes. */
  readonly maxInputChars = SESSION_HUB_MAX_INPUT_CHARS;
  readonly #now: () => number;
  readonly #wallNow: () => number;
  readonly #sleep: (seconds: number) => Promise<void>;

  constructor(options: SessionHubOptions = {}) {
    const now = options.now ?? monotonicNow;
    const wallNow = options.wallNow ?? (() => Date.now() / 1000);
    this.#now = now;
    this.#wallNow = wallNow;
    this.#sleep = options.sleep ?? realSleep;

    // The hub's own monotonic clock, not the wall one: a bucket that refilled
    // against wall time would hand a flooding client a full budget the moment
    // the system clock stepped forward.
    this.limiter = new RateLimiter({
      restAcquireRate: options.restAcquireRate ?? SESSION_HUB_REST_ACQUIRE_RATE,
      restSendRate: options.restSendRate ?? SESSION_HUB_REST_SEND_RATE,
      now,
    });
    this.store = new StateStore({
      registry: this.registry,
      maxBufferChars: SESSION_HUB_MAX_BUFFER_CHARS,
      now,
    });
    // Every service below reaches back through `this`, so each callback is a
    // method reference rather than a copy of the predicate — one answer to
    // "is this hijacked?", not five that can drift apart.
    this.router = new MessageRouter({
      hub: {
        registry: this.registry,
        startupPendingBrowsers: this.startupPendingBrowsers,
        isHijacked: (state) => this.store.isHijacked(state),
        isDashboardHijackActive: (state) => this.store.isDashboardHijackActive(state),
        hasValidRestLease: (state) => this.store.hasValidRestLease(state),
        removeDeadBrowsers: (workerId, dead) => this.lease.removeDeadBrowsers(workerId, dead),
      },
    });
    this.presence = new PresenceManager({
      hub: {
        registry: this.registry,
        isHijacked: (state) => this.store.isHijacked(state),
        isDashboardHijackActive: (state) => this.store.isDashboardHijackActive(state),
        // No browser can attach — see the note at the top of this file — so
        // every connection asked about is one the hub has never seen, and the
        // reference's answer for that is `viewer`.
        resolveRoleForBrowser: async () => "viewer",
        sendWorker: (workerId, message) => this.sendWorker(workerId, message),
      },
      wallNow,
    });
    this.lease = new HijackLeaseManager({
      registry: this.registry,
      hub: {
        isHijacked: (state) => this.store.isHijacked(state),
        isDashboardHijackActive: (state) => this.store.isDashboardHijackActive(state),
        hasValidRestLease: (state) => this.store.hasValidRestLease(state),
        canSendInput: (state, ws) => this.presence.canSendInput(state, ws),
        metric: (name) => {
          this.store.metric(name);
        },
        notifyHijackChanged: (workerId, enabled, owner) => {
          this.store.notifyHijackChanged(workerId, { enabled, owner });
        },
        sendWorker: (workerId, message) => this.sendWorker(workerId, message),
        broadcastHijackState: (workerId) => this.router.broadcastHijackState(workerId),
        appendEvent: (workerId, eventType) => this.appendEvent(workerId, eventType),
        pruneIfIdle: (workerId) => this.pruneIfIdle(workerId),
        recheckAndResume: (workerId, at) => this.lease.recheckAndResume(workerId, at),
      },
      dashboardLeaseSeconds: SESSION_HUB_DASHBOARD_LEASE_S,
      now,
      wallNow,
    });
    this.polling = new PollingCoordinator({
      hub: {
        registry: this.registry,
        requestSnapshot: (workerId) => this.presence.requestSnapshot(workerId),
        monotonic: now,
        sleep: this.#sleep,
      },
      wallNow,
    });
    this.connections = new ConnectionManager({
      hub: {
        registry: this.registry,
        startupPendingBrowsers: this.startupPendingBrowsers,
        maxWorkers: SESSION_HUB_MAX_WORKERS,
        maxConnectionsPerPrincipal: SESSION_HUB_MAX_CONNECTIONS_PER_PRINCIPAL,
        isHijacked: (state) => this.store.isHijacked(state),
        isDashboardHijackActive: (state) => this.store.isDashboardHijackActive(state),
        hasValidRestLease: (state) => this.store.hasValidRestLease(state),
      },
      now,
    });
  }

  /** Clamp a requested lease into the range this hub grants. */
  clampLease(leaseSeconds: number): number {
    return clampLease(leaseSeconds);
  }

  /** Monotonic seconds, as every lease deadline is measured in. */
  monotonic(): number {
    return this.#now();
  }

  /** Wall seconds, as every timestamp that leaves the process is in. */
  wallNow(): number {
    return this.#wallNow();
  }

  /**
   * A lease deadline, converted for whoever is going to read it.
   *
   * Leases are held monotonically so a system clock adjustment cannot make one
   * look renewed or long dead; a client compares the number against its own
   * wall clock, so the offset between the two is applied here at the boundary.
   */
  monoToWall(monoTs: number): number {
    return this.#wallNow() + (monoTs - this.#now());
  }

  /**
   * Send one message to a worker.
   *
   * Reports whether there was anybody to send it to, which is what separates
   * "the worker refused" from "there is no worker" at every call site above.
   * A socket that throws is treated as gone: the reference's own reading, and
   * the alternative is a hub that keeps addressing a closed connection.
   */
  async sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean> {
    const socket = this.registry.get(workerId)?.workerWs;
    if (socket === undefined) {
      return false;
    }
    try {
      await socket.sendText(encodeWorkerFrame(message));
    } catch {
      return false;
    }
    return true;
  }

  /**
   * Append one event to a worker's log, and return it.
   *
   * A worker that has gone still gets an event *object* back, with sequence
   * zero and nothing stored. The reference does the same: the caller is
   * usually mid-teardown and has no better answer to give, and raising here
   * would turn a disconnect into an error on a path that was tidying up.
   */
  async appendEvent(
    workerId: string,
    eventType: string,
    data: Record<string, unknown> = {},
  ): Promise<Record<string, unknown>> {
    const state = this.registry.get(workerId);
    if (state === undefined) {
      return { seq: 0, ts: this.#wallNow(), type: eventType, data };
    }
    state.eventSeq += 1;
    const event = { seq: state.eventSeq, ts: this.#wallNow(), type: eventType, data };
    state.events.push(event);
    // The oldest retained sequence, re-read rather than counted: the log drops
    // its front when it overflows, and a counter would not know that happened.
    state.minEventSeq = Number((state.events.at(0) as Record<string, unknown>).seq);
    return event;
  }

  /** Store the most recent screen a worker sent. */
  async updateLastSnapshot(workerId: string, snapshot: Record<string, unknown>): Promise<void> {
    const state = this.registry.get(workerId);
    if (state !== undefined) {
      state.lastSnapshot = snapshot;
    }
  }

  /** The most recent screen a worker sent, or nothing when it never sent one. */
  async getLastSnapshot(workerId: string): Promise<Record<string, unknown> | undefined> {
    return this.registry.get(workerId)?.lastSnapshot;
  }

  /**
   * Move a worker between `open` and `hijack`.
   *
   * Refuses to open a session somebody is holding: in open mode the lease
   * stops gating input, so opening one mid-hijack would let every operator
   * type into a terminal one of them believes they alone are driving.
   *
   * @returns The reference's `(ok, reason)` pair — `not_found` for a worker
   *   this hub does not know, `active_hijack` for one that is held.
   */
  async setInputMode(workerId: string, mode: InputMode): Promise<{ ok: boolean; reason?: string }> {
    const state = this.registry.get(workerId);
    if (state === undefined) {
      return { ok: false, reason: "not_found" };
    }
    if (mode === "open" && this.store.isHijacked(state)) {
      return { ok: false, reason: "active_hijack" };
    }
    state.inputMode = mode;
    // Every caller of this is an authenticated route — the session routes and
    // the worker-control route, which requires `session.control.mode`. Reaching
    // here therefore means somebody *decided* the mode, and a later
    // `worker_hello` may raise it but never lower it back.
    state.inputModeSetByOperator = true;
    await this.router.broadcast(workerId, { type: "input_mode_changed", input_mode: mode, ts: this.#wallNow() });
    await this.router.broadcastHijackState(workerId);
    return { ok: true };
  }

  /**
   * Clear whatever is held on a worker and let it run again.
   *
   * The one release nobody asked for: it is what a session being opened to
   * everyone does to the lease that was gating it, and what a teardown does on
   * the way out. Reports whether anything was actually held, so a caller can
   * tell a release from a no-op.
   */
  async forceReleaseHijack(workerId: string): Promise<boolean> {
    const state = this.registry.get(workerId);
    if (state === undefined) {
      return false;
    }
    let owner = "server-forced";
    let held = false;
    if (state.hijackSession !== undefined) {
      owner = state.hijackSession.owner;
      state.hijackSession = undefined;
      held = true;
    }
    if (this.store.isDashboardHijackActive(state)) {
      state.hijackOwner = undefined;
      state.hijackOwnerExpiresAt = undefined;
      held = true;
    }
    if (!held) {
      return false;
    }
    await this.sendWorker(workerId, {
      type: "control",
      action: "resume",
      owner,
      lease_s: 0,
      ts: this.#wallNow(),
    });
    this.store.notifyHijackChanged(workerId, { enabled: false });
    await this.router.broadcastHijackState(workerId);
    return true;
  }

  /** Forget a worker that has no socket, no browsers and no lease. */
  async pruneIfIdle(workerId: string): Promise<void> {
    const state = this.registry.get(workerId);
    if (state === undefined) {
      return;
    }
    if (
      state.workerWs === undefined &&
      state.browsers.size === 0 &&
      state.hijackOwner === undefined &&
      state.hijackSession === undefined
    ) {
      this.registry.pop(workerId);
    }
  }

  /** Attach `socket` as the worker for `workerId`, in `mode`. */
  registerWorker(workerId: string, socket: WorkerSocket, mode: InputMode): void {
    this.connections.registerWorker(workerId, socket);
    this.connections.setWorkerHello(workerId, mode);
  }
}
