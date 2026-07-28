//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Who is attached to a worker, in what role, and what they may do.
 *
 * Port of the Python module `provide.uterm.server.bridge.hub.presence` and
 * the Go package `hub` (`presence.go`).
 *
 * Read-only apart from the two worker-bound pokes at the end, which ask the
 * worker for a fresh snapshot or analysis — presence-shaped operations that
 * happen to travel over the worker socket rather than a browser one.
 */

import type { BrowserRole, Connection, InputMode, WorkerTermState } from "./models.ts";
import type { WorkerRegistry } from "./registry.ts";

/** The hub surface the presence manager reaches back through. */
export interface PresenceHubCallbacks {
  /** The worker table presence reads. */
  registry: WorkerRegistry<WorkerTermState>;
  /** Whether any hijack is active on `state`. */
  isHijacked(state: WorkerTermState): boolean;
  /** Whether a dashboard hijack is active on `state`. */
  isDashboardHijackActive(state: WorkerTermState): boolean;
  /** Resolve the role a browser holds on a worker. */
  resolveRoleForBrowser(ws: Connection, workerId: string): Promise<BrowserRole>;
  /** Send a message to the worker. */
  sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean>;
}

/** Construction options for {@link PresenceManager}. */
export interface PresenceManagerOptions {
  /** Cross-cutting queries and sends, injected to avoid importing the hub. */
  hub: PresenceHubCallbacks;
  /** Wall clock in seconds, for the timestamps the worker shows a human. */
  wallNow?: () => number;
  /** Request-id source; injected so a test can pin it. */
  newRequestId?: () => string;
}

/** What a resuming browser is told about the session it is rejoining. */
export interface BrowserStateSnapshot {
  /** Whether anyone holds the worker. */
  isHijacked: boolean;
  /** Whether *this* browser is the one holding it. */
  hijackedByMe: boolean;
  /** Whether the worker itself is connected. */
  workerOnline: boolean;
  /** Whether input is gated behind the lease or open to operators. */
  inputMode: InputMode;
}

/** Browser-presence queries and the worker-bound presence pokes. */
export class PresenceManager {
  readonly #hub: PresenceHubCallbacks;
  readonly #wallNow: () => number;
  readonly #newRequestId: () => string;

  constructor(options: PresenceManagerOptions) {
    this.#hub = options.hub;
    this.#wallNow = options.wallNow ?? (() => Date.now() / 1000);
    this.#newRequestId = options.newRequestId ?? (() => crypto.randomUUID());
  }

  /**
   * The current session state, from `ws`'s point of view.
   *
   * A worker the hub does not know answers all-false rather than failing: a
   * browser can attach before its worker connects, and an error there would
   * break the page instead of showing an empty session.
   */
  async registerBrowserStateSnapshot(workerId: string, ws: Connection): Promise<BrowserStateSnapshot> {
    const state = this.#hub.registry.get(workerId);
    if (state === undefined) {
      return { isHijacked: false, hijackedByMe: false, workerOnline: false, inputMode: "hijack" };
    }
    return {
      isHijacked: this.#hub.isHijacked(state),
      hijackedByMe: this.#hub.isDashboardHijackActive(state) && state.hijackOwner === ws,
      workerOnline: state.workerWs !== undefined,
      inputMode: state.inputMode,
    };
  }

  /** The role `ws` holds on `workerId`, via the hub's configured resolver. */
  async resolveRoleForBrowser(ws: Connection, workerId: string): Promise<BrowserRole> {
    return this.#hub.resolveRoleForBrowser(ws, workerId);
  }

  /**
   * Whether `ws` may send input to `state`.
   *
   * This runs on every browser input frame, so it stays a couple of lookups
   * with no allocation.
   *
   * The two modes ask different questions. In hijack mode only the lease
   * holder may type. In open mode the lease stops mattering entirely and the
   * *role* decides — viewers are still refused, and holding the lease does
   * not exempt anyone from that. A browser the hub has never seen counts as a
   * viewer rather than being refused outright.
   */
  canSendInput(state: WorkerTermState, ws: Connection): boolean {
    if (state.inputMode === "open") {
      const role = state.browsers.get(ws) ?? "viewer";
      return role === "operator" || role === "admin";
    }
    return this.#hub.isDashboardHijackActive(state) && state.hijackOwner === ws;
  }

  /** Ask the worker for a fresh screen snapshot. */
  async requestSnapshot(workerId: string): Promise<void> {
    await this.#poke(workerId, "snapshot_req");
  }

  /** Ask the worker for a fresh analysis pass. */
  async requestAnalysis(workerId: string): Promise<void> {
    await this.#poke(workerId, "analyze_req");
  }

  /**
   * Send a request frame to the worker.
   *
   * Each carries its own id: the worker correlates its reply by it, so two
   * outstanding pokes sharing one would have their answers confused. A worker
   * that is not connected simply drops it — these are requests, not commands.
   */
  async #poke(workerId: string, type: string): Promise<void> {
    await this.#hub.sendWorker(workerId, {
      type,
      req_id: this.#newRequestId(),
      ts: this.#wallNow(),
    });
  }
}
