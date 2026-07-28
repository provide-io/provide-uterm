//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Outbound frame plumbing: the hub's busiest path.
 *
 * Port of the Python modules
 * `provide.uterm.server.bridge.hub.router_impl` and `...router_broadcast`,
 * and the Go package `hub` (`router*.go`).
 *
 * Broadcast runs once per outbound terminal frame, so the shape here is
 * about not letting one bad browser cost the others: sends fan out
 * concurrently, each with its own timeout, and whatever fails is collected
 * and cleaned up in one pass afterwards.
 */

import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import { makeHijackStateFrame } from "../frames/index.ts";
import { monoToWall } from "./frames.ts";
import type { Connection, WorkerTermState } from "./models.ts";
import type { WorkerRegistry } from "./registry.ts";

/**
 * How long one browser send may take before it counts as dead.
 *
 * A browser that cannot accept a frame within this is not slow, it is gone —
 * and holding the session open for it would keep its lease alive.
 */
export const BROADCAST_SEND_TIMEOUT_S = 5;

/** A browser connection the router can write to. */
interface BrowserSocket extends Connection {
  sendText(payload: string): Promise<void>;
}

/** The hub surface the router reaches back through. */
export interface RouterHubCallbacks {
  /** The worker table broadcasts read. */
  registry: WorkerRegistry<WorkerTermState>;
  /** Browsers that have connected but not finished their handshake. */
  startupPendingBrowsers: Set<Connection>;
  /** Whether any hijack is active on `state`. */
  isHijacked(state: WorkerTermState): boolean;
  /** Whether a dashboard hijack is active on `state`. */
  isDashboardHijackActive(state: WorkerTermState): boolean;
  /** Whether an unexpired REST lease is held on `state`. */
  hasValidRestLease(state: WorkerTermState): boolean;
  /** Drop dead sockets, reporting whether the hijack state changed. */
  removeDeadBrowsers(workerId: string, dead: Set<Connection>): Promise<boolean>;
}

/** Construction options for {@link MessageRouter}. */
export interface MessageRouterOptions {
  /** Cross-cutting state and cleanup, injected to avoid importing the hub. */
  hub: RouterHubCallbacks;
  /** Per-send timeout; defaults to {@link BROADCAST_SEND_TIMEOUT_S}. */
  sendTimeoutS?: number;
}

/**
 * Encode a message for a browser socket.
 *
 * A `term` message is raw terminal data; everything else is a framed control
 * envelope. The mirror image of the worker-bound dispatch, with the same
 * consequence reversed — a control frame sent down the terminal path would
 * render its JSON onto the user's screen.
 */
export function encodeBrowserFrame(message: Record<string, unknown>): string {
  if (String(message["type"] ?? "") === "term") {
    return encodeTerminalData(String(message["data"] ?? ""));
  }
  return encodeControlFrame(message);
}

/**
 * The owner label for a hijack-state frame, from one browser's point of view.
 *
 * Three values: `me` for the holder, `other` for anyone else, nothing for
 * nobody. It is per-recipient, so a single broadcast produces different
 * frames for different browsers.
 *
 * A REST lease is always `other`: no browser holds it, and a stale socket
 * left in the owner slot must not make that browser's UI believe it is in
 * control.
 */
export function hijackOwnerLabel(isDashboard: boolean, isRest: boolean, holdsLease: boolean): string | undefined {
  if (isDashboard && holdsLease) {
    return "me";
  }
  if (isDashboard || isRest) {
    return "other";
  }
  return undefined;
}

/** Resolve once a promise settles or `seconds` elapse, whichever is first. */
async function withTimeout(work: Promise<void>, seconds: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expiry = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error("send timed out")), seconds * 1000);
  });
  try {
    await Promise.race([work, expiry]);
  } finally {
    clearTimeout(timer);
  }
}

/** Outbound frame plumbing for the hub. */
export class MessageRouter {
  readonly #hub: RouterHubCallbacks;
  readonly #sendTimeoutS: number;

  constructor(options: MessageRouterOptions) {
    this.#hub = options.hub;
    this.#sendTimeoutS = options.sendTimeoutS ?? BROADCAST_SEND_TIMEOUT_S;
  }

  /**
   * Send `message` to every browser attached to `workerId`.
   *
   * Sends fan out concurrently: sequentially, one stalled browser would make
   * every browser behind it wait out the whole timeout before seeing a
   * frame. Failures are collected rather than raised, then cleaned up in one
   * pass — and if losing them changed who holds the session, the survivors
   * are told.
   */
  async broadcast(workerId: string, message: Record<string, unknown>): Promise<void> {
    const state = this.#hub.registry.get(workerId);
    if (state === undefined) {
      return;
    }
    const recipients = this.#recipients(state);
    const payload = encodeBrowserFrame(message);

    const dead = await this.#sendAll(recipients, () => payload);
    if (dead.size === 0) {
      return;
    }
    const changed = await this.#hub.removeDeadBrowsers(workerId, dead);
    if (changed) {
      await this.broadcastHijackState(workerId);
    }
  }

  /**
   * Tell every browser attached to `workerId` who holds the session.
   *
   * Each browser gets its own frame, because the answer to "is it mine?"
   * differs per recipient. Dead sockets are dropped and the survivors are
   * told again, since losing a browser can itself change the answer.
   */
  async broadcastHijackState(workerId: string): Promise<void> {
    const dead = await this.#sendHijackState(workerId);
    if (dead === undefined || dead.size === 0) {
      return;
    }
    await this.#hub.removeDeadBrowsers(workerId, dead);
    // A second pass for whoever is left: the browser that went away may have
    // been the one holding the session.
    await this.#sendHijackState(workerId, true);
  }

  /** Browsers eligible to receive a frame right now. */
  #recipients(state: WorkerTermState): BrowserSocket[] {
    // A browser mid-handshake has not been told what session it is joining;
    // terminal output arriving first would render before the screen state.
    return [...state.browsers.keys()].filter((ws) => !this.#hub.startupPendingBrowsers.has(ws)) as BrowserSocket[];
  }

  /** Send to everyone concurrently, returning whoever failed. */
  async #sendAll(recipients: BrowserSocket[], payloadFor: (ws: BrowserSocket) => string): Promise<Set<Connection>> {
    const dead = new Set<Connection>();
    await Promise.all(
      recipients.map(async (ws) => {
        try {
          await withTimeout(ws.sendText(payloadFor(ws)), this.#sendTimeoutS);
        } catch {
          dead.add(ws);
        }
      }),
    );
    return dead;
  }

  /**
   * Send one pass of hijack-state frames.
   *
   * Returns the sockets that failed, or nothing when the worker has gone —
   * which can happen between the two passes, since the first one awaits.
   */
  async #sendHijackState(workerId: string, suppressErrors = false): Promise<Set<Connection> | undefined> {
    const state = this.#hub.registry.get(workerId);
    if (state === undefined) {
      return undefined;
    }
    const recipients = this.#recipients(state);
    const isDashboard = this.#hub.isDashboardHijackActive(state);
    const isRest = this.#hub.hasValidRestLease(state);
    const hijacked = this.#hub.isHijacked(state);
    const owner = state.hijackOwner;
    // A live REST lease is the one that governs the deadline shown; the
    // dashboard expiry only applies when no REST lease is held.
    const leaseExpiresAt =
      isRest && state.hijackSession !== undefined ? state.hijackSession.leaseExpiresAt : state.hijackOwnerExpiresAt;

    const dead = await this.#sendAll(recipients, (ws) =>
      encodeBrowserFrame(
        makeHijackStateFrame({
          hijacked,
          owner: hijackOwnerLabel(isDashboard, isRest, owner === ws) ?? null,
          leaseExpiresAt: monoToWall(leaseExpiresAt) ?? null,
          inputMode: state.inputMode,
        }) as unknown as Record<string, unknown>,
      ),
    );
    return suppressErrors ? new Set<Connection>() : dead;
  }
}
