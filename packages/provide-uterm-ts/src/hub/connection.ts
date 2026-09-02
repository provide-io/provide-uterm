//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Worker and browser connection lifecycle.
 *
 * Port of the Python module `provide.uterm.server.bridge.hub.connection` and
 * the Go package `hub` (`connection*.go`).
 *
 * Three of the decisions here carry a scar, and each is noted where it is
 * made: a worker reconnect must not invalidate a live lease, the worker cap
 * counts new ids only, and the per-principal browser quota has to balance
 * exactly even when registration fails halfway through.
 */

import { type BrowserRole, type Connection, type InputMode, type WorkerSocket, WorkerTermState } from "./models.ts";
import type { WorkerRegistry } from "./registry.ts";
import { encodeBrowserFrame } from "./router.ts";

/** Raised when the hub is already holding as many workers as it will. */
export class WorkerCapacityError extends Error {
  constructor() {
    super("worker capacity exceeded");
    this.name = "WorkerCapacityError";
  }
}

/** Raised when a principal already holds as many browsers as it may. */
export class ConnectionQuotaError extends Error {
  constructor() {
    super("too many connections");
    this.name = "ConnectionQuotaError";
  }
}

/** Event types that mean a resume frame has already gone out. */
const RESUME_ALREADY_SENT = new Set(["hijack_owner_expired", "hijack_lease_expired"]);

/** Event types that end the backwards scan without answering it. */
const RESUME_SCAN_STOPS = new Set(["hijack_acquired", "hijack_released"]);

/** The hub surface the connection manager reaches back through. */
export interface ConnectionHubCallbacks {
  /** The worker table connections are registered into. */
  registry: WorkerRegistry<WorkerTermState>;
  /** Browsers that have connected but not finished their handshake. */
  startupPendingBrowsers: Set<Connection>;
  /** What each of those browsers has missed, in arrival order. */
  startupPendingFrames: Map<Connection, Record<string, unknown>[]>;
  /** How many distinct workers the hub will hold. */
  maxWorkers: number;
  /** How many browsers one authenticated principal may hold. */
  maxConnectionsPerPrincipal: number;
  /** Whether any hijack is active on `state`. */
  isHijacked(state: WorkerTermState): boolean;
  /** Whether a dashboard hijack is active on `state`. */
  isDashboardHijackActive(state: WorkerTermState): boolean;
  /** Whether an unexpired REST lease is held on `state`. */
  hasValidRestLease(state: WorkerTermState): boolean;
}

/** Construction options for {@link ConnectionManager}. */
export interface ConnectionManagerOptions {
  /** Cross-cutting state and predicates, injected to avoid importing the hub. */
  hub: ConnectionHubCallbacks;
  /** Monotonic clock in seconds. */
  now?: () => number;
  /** Mints a resume token, when a resume store is configured. */
  createResumeToken?: (workerId: string, role: BrowserRole) => Promise<string>;
}

/** What a browser is told about the session it has just joined. */
export interface BrowserRegistration {
  /** Whether anyone holds the worker. */
  isHijacked: boolean;
  /** Whether this browser is the one holding it. */
  hijackedByMe: boolean;
  /** Whether the worker itself is connected. */
  workerOnline: boolean;
  /** Whether input is gated behind the lease or open to operators. */
  inputMode: InputMode;
  /** The last screen the worker sent, so the page renders immediately. */
  initialSnapshot?: Record<string, unknown> | undefined;
  /** Token this browser can reconnect with, when resume is configured. */
  resumeToken?: string | undefined;
}

/** Options for {@link ConnectionManager.registerBrowser}. */
export interface RegisterBrowserOptions {
  /** Hold broadcasts back until the startup frames have been sent. */
  deferBroadcast?: boolean;
}

/** The outcome of a browser disconnect. */
export interface DisconnectOutcome {
  /** Whether the departing browser held the dashboard lease. */
  wasOwner: boolean;
  /** Whether a REST lease is still live. */
  restStillActive: boolean;
  /** Whether the worker still needs a resume frame. */
  resumeWithoutOwner: boolean;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/**
 * Whether a resume frame is still owed, from the worker's event history.
 *
 * Scans backwards and stops at the first hijack lifecycle event. Checking
 * only the newest event is fragile: a snapshot arriving after an expiry
 * would hide the marker and cause a second resume to be sent.
 *
 * An acquire or release ends the scan without answering — the session was
 * taken or given up since, so anything older says nothing about now.
 */
export function scanEventsForResume(state: WorkerTermState): boolean {
  const events = state.events.toArray();
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const eventType = events[index]?.type;
    if (typeof eventType !== "string") {
      continue;
    }
    if (RESUME_ALREADY_SENT.has(eventType)) {
      return false;
    }
    if (RESUME_SCAN_STOPS.has(eventType)) {
      break;
    }
  }
  return true;
}

/** The subject id a browser should be counted against, if any. */
function principalSubjectId(ws: Connection): string | undefined {
  const principal = (ws as { state?: { utermPrincipal?: { subjectId?: unknown } } }).state?.utermPrincipal;
  const subjectId = principal?.subjectId;
  // Anonymous and unauthenticated connections are the auth layer's problem,
  // not the quota's.
  if (typeof subjectId !== "string" || subjectId === "" || subjectId === "anonymous") {
    return undefined;
  }
  return subjectId;
}

/** Worker and browser connection lifecycle. */
export class ConnectionManager {
  readonly #hub: ConnectionHubCallbacks;
  readonly #now: () => number;
  readonly #createResumeToken: ((workerId: string, role: BrowserRole) => Promise<string>) | undefined;
  /**
   * Which browsers each authenticated principal currently holds.
   *
   * One structure rather than a counter plus a reverse map: the two can only
   * drift apart if they are maintained separately, and a drifted counter is
   * unrecoverable — nothing reaps it, so the principal stays locked out.
   */
  readonly #principalBrowsers = new Map<string, Set<Connection>>();
  /** Which principal each browser was counted against. */
  readonly #wsPrincipal = new Map<Connection, string>();
  /** Resume tokens minted per browser. */
  readonly #wsResumeToken = new Map<Connection, string>();

  constructor(options: ConnectionManagerOptions) {
    this.#hub = options.hub;
    this.#now = options.now ?? monotonicNow;
    this.#createResumeToken = options.createResumeToken;
  }

  /**
   * Register `ws` as the worker for `workerId`.
   *
   * Reports whether a hijack was cleared, so the caller can tell the browsers.
   *
   * A lease is cleared **only when it has actually expired**. Worker sockets
   * drop routinely — a Durable Object rotating, a manager restarting, a
   * network blip — and clearing the hijack on every register meant one blip
   * silently invalidated the holder's hijack id, every later send 404'd, and
   * a long run cratered. Time-bounded expiry is the security guarantee; a
   * reconnect is not a security event.
   *
   * @throws {WorkerCapacityError} When a *new* worker id arrives at capacity.
   *   A known id is always readmitted, or the cap would leave a full hub
   *   unable to heal.
   */
  registerWorker(workerId: string, ws: WorkerSocket): boolean {
    const known = this.#hub.registry.contains(workerId);
    if (!known && this.#hub.registry.size >= this.#hub.maxWorkers) {
      throw new WorkerCapacityError();
    }
    const state = this.#hub.registry.setDefault(workerId, new WorkerTermState({ now: this.#now }));

    const now = this.#now();
    const session = state.hijackSession;
    const expired = session !== undefined && session.leaseExpiresAt <= now;
    const prevWasHijacked = expired || (session === undefined && state.hijackOwner !== undefined);
    if (expired) {
      state.hijackSession = undefined;
    }
    if (prevWasHijacked) {
      state.hijackOwner = undefined;
      state.hijackOwnerExpiresAt = undefined;
    }
    state.workerWs = ws;
    return prevWasHijacked;
  }

  /**
   * Clear `ws` as the worker for `workerId`, if it is still the current one.
   *
   * A socket a replacement has already taken over from is left alone: the
   * replacement is the live worker, and tearing its state down because the
   * old socket finally noticed it had closed would disconnect it.
   */
  deregisterWorker(workerId: string, ws: WorkerSocket): { shouldBroadcast: boolean; wasHijacked: boolean } {
    const state = this.#hub.registry.get(workerId);
    if (state === undefined || state.workerWs !== ws) {
      return { shouldBroadcast: false, wasHijacked: false };
    }
    const wasHijacked = state.hijackSession !== undefined || state.hijackOwner !== undefined;
    state.workerWs = undefined;
    state.hijackSession = undefined;
    state.hijackOwner = undefined;
    state.hijackOwnerExpiresAt = undefined;
    return { shouldBroadcast: true, wasHijacked };
  }

  /**
   * Apply a `worker_hello`: set the input mode and record the protocol version.
   *
   * Refuses to open input while a session is held — that would let every
   * operator type into a session someone else is driving.
   */
  setWorkerHello(workerId: string, mode: InputMode, protocolVersion?: number): boolean {
    const state = this.#hub.registry.get(workerId);
    if (state === undefined) {
      return false;
    }
    // A hello may raise the mode, never lower a decided one. Two reasons to
    // refuse and both are needed: a lease is actually held, or somebody decided
    // the mode through an authenticated route. The second is the window the
    // lease check alone left open — an operator sets `hijack` and then acquires,
    // and a hello landing between those steps reverted the mode, so the acquire
    // was refused for being in open mode and the operator's only clue was a
    // failure that looked like their own mistake.
    //
    // Keyed on whether the hello would actually lower the mode rather than on
    // its value, so a hello agreeing with a decided `open` is not a downgrade.
    const wouldLower = mode === "open" && state.inputMode === "hijack";
    if (wouldLower && (state.inputModeSetByOperator || this.#hub.isHijacked(state))) {
      return false;
    }
    state.inputMode = mode;
    if (protocolVersion !== undefined) {
      state.protocolVersion = protocolVersion;
    }
    return true;
  }

  /**
   * Register `ws` as a browser and describe the session it has joined.
   *
   * The quota is taken *before* anything else, so a rejected connection never
   * mints a resume token it will not use. Everything after the increment is
   * rolled back on failure: a leaked slot is unrecoverable, because nothing
   * reaps the counter and the principal stays locked out at their limit.
   *
   * @throws {ConnectionQuotaError} When the principal is at their limit.
   */
  async registerBrowser(
    workerId: string,
    ws: Connection,
    role: BrowserRole,
    options: RegisterBrowserOptions = {},
  ): Promise<BrowserRegistration> {
    const subjectId = principalSubjectId(ws);
    if (subjectId !== undefined) {
      const held = this.#principalBrowsers.get(subjectId) ?? new Set<Connection>();
      if (held.size >= this.#hub.maxConnectionsPerPrincipal) {
        throw new ConnectionQuotaError();
      }
      held.add(ws);
      this.#principalBrowsers.set(subjectId, held);
      this.#wsPrincipal.set(ws, subjectId);
    }

    try {
      let resumeToken: string | undefined;
      if (this.#createResumeToken !== undefined) {
        resumeToken = await this.#createResumeToken(workerId, role);
        this.#wsResumeToken.set(ws, resumeToken);
      }
      const state = this.#hub.registry.setDefault(workerId, new WorkerTermState({ now: this.#now }));
      state.browsers.set(ws, role);
      if (options.deferBroadcast === true) {
        this.#hub.startupPendingBrowsers.add(ws);
      }
      return {
        isHijacked: this.#hub.isHijacked(state),
        hijackedByMe: this.#hub.isDashboardHijackActive(state) && state.hijackOwner === ws,
        workerOnline: state.workerWs !== undefined,
        inputMode: state.inputMode,
        initialSnapshot: state.lastSnapshot,
        ...(resumeToken === undefined ? {} : { resumeToken }),
      };
    } catch (error) {
      this.#releaseQuota(ws);
      throw error;
    }
  }

  /**
   * Let broadcasts reach `ws` now that its startup frames have gone out,
   * delivering whatever was broadcast while it was still starting up.
   *
   * The socket stays pending until its queue drains. Releasing it first and
   * then flushing would let a frame broadcast mid-flush overtake the ones
   * already waiting, which reorders the very list this exists to keep intact.
   *
   * A socket whose flush fails is left pending: pending means the broadcast
   * path skips it, which is the right resting state for a connection that just
   * failed a write, and the disconnect handler clears both.
   */
  async activateBrowserBroadcasts(workerId: string, ws: Connection): Promise<void> {
    for (;;) {
      const batch = this.#hub.startupPendingFrames.get(ws) ?? [];
      if (batch.length === 0) {
        this.#hub.startupPendingFrames.delete(ws);
        const state = this.#hub.registry.get(workerId);
        // The browser can disconnect between its startup frames and this call;
        // one that did is left pending on purpose.
        if (state?.browsers.has(ws) === true) {
          this.#hub.startupPendingBrowsers.delete(ws);
        }
        return;
      }
      this.#hub.startupPendingFrames.set(ws, []);
      // Cast rather than guard: the router already treats `state.browsers` as
      // sockets that can be written to, and one that cannot is a dead socket
      // either way -- calling through lands in the catch below, which is the
      // resting state such a connection should be left in.
      const sender = ws as { sendText: (payload: string) => Promise<void> };
      for (const message of batch) {
        try {
          await sender.sendText(encodeBrowserFrame(message));
        } catch {
          // Left pending, backlog dropped: the socket just failed a write.
          this.#hub.startupPendingFrames.delete(ws);
          return;
        }
      }
    }
  }

  /**
   * Handle a browser disconnecting.
   *
   * Reports what the caller has to do about it: whether the departing browser
   * held the lease, whether a REST lease survives it, and whether the worker
   * is still owed a resume frame.
   */
  cleanupBrowserDisconnect(workerId: string, ws: Connection, ownedHijack: boolean): DisconnectOutcome {
    const state = this.#hub.registry.get(workerId);
    let wasOwner = false;
    let restStillActive = false;
    let resumeWithoutOwner = false;

    if (state !== undefined) {
      wasOwner = this.#hub.isDashboardHijackActive(state) && state.hijackOwner === ws;
      state.browsers.delete(ws);
      if (wasOwner) {
        state.hijackOwner = undefined;
        state.hijackOwnerExpiresAt = undefined;
        restStillActive = this.#hub.hasValidRestLease(state);
      } else if (ownedHijack && state.workerWs !== undefined && !this.#hub.isHijacked(state)) {
        resumeWithoutOwner = scanEventsForResume(state);
      }
    }
    this.#releaseQuota(ws);
    this.#hub.startupPendingBrowsers.delete(ws);
    this.#hub.startupPendingFrames.delete(ws);
    return { wasOwner, restStillActive, resumeWithoutOwner };
  }

  /** How many browsers `subjectId` currently holds, for inspection. */
  principalConnectionCount(subjectId: string): number {
    return this.#principalBrowsers.get(subjectId)?.size ?? 0;
  }

  /**
   * Give back whatever `ws` was counted against.
   *
   * The exact inverse of the increment, used both by a failed registration
   * and by a clean disconnect, so the two can never drift apart.
   */
  #releaseQuota(ws: Connection): void {
    this.#wsResumeToken.delete(ws);
    const subjectId = this.#wsPrincipal.get(ws);
    if (subjectId === undefined) {
      return;
    }
    this.#wsPrincipal.delete(ws);
    const held = this.#principalBrowsers.get(subjectId);
    held?.delete(ws);
    if (held?.size === 0) {
      // Drop the empty set rather than leaving it: a hub that has seen many
      // principals would otherwise keep one entry each, forever.
      this.#principalBrowsers.delete(subjectId);
    }
  }
}
