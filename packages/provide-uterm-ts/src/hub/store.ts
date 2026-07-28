//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Input buffering, worker lifecycle and the hijack-state predicates.
 *
 * Port of the Python module `provide.uterm.server.bridge.hub.store` and the Go
 * package `hub` (`store.go`).
 *
 * The reference reaches these through a back reference to the composing hub;
 * this takes the same surface as explicit options, which is what the Go port
 * does and what makes the service testable without standing up a hub.
 */

import { type Logger, noopLogger } from "../telemetry/index.ts";
import { type Connection, WorkerTermState } from "./models.ts";
import type { WorkerRegistry } from "./registry.ts";

/** Shortest lease the hub will grant, in seconds. */
export const LEASE_MIN_SECONDS = 1;

/**
 * Longest lease the hub will grant, in seconds — four hours.
 *
 * Matched to the WebSocket idle-reader timeout so a long operator hold is not
 * killed by whichever of the two fires first. The earlier one-hour cap was too
 * tight for multi-hour runs that drive one target hard while others idle.
 */
export const LEASE_MAX_SECONDS = 14400;

/**
 * Clamp a requested lease duration into the accepted range.
 *
 * Truncates toward zero first, matching the reference's `int()` coercion: a
 * request for 90.9 seconds grants 90, never 91.
 */
export function clampLease(leaseSeconds: number): number {
  return Math.max(LEASE_MIN_SECONDS, Math.min(Math.trunc(leaseSeconds), LEASE_MAX_SECONDS));
}

/** Construction options for {@link StateStore}. */
export interface StateStoreOptions {
  /** Worker table this store creates into and stamps activity on. */
  registry: WorkerRegistry<WorkerTermState>;
  /** Cap on a single browser's un-terminated input buffer. */
  maxBufferChars: number;
  /** Monotonic clock in seconds. */
  now?: () => number;
  /** Where callback failures are reported. */
  logger?: Logger;
  /** Metrics sink, if one is configured. */
  onMetric?: (name: string, value: number) => void;
  /** Notified when a worker's hijack state changes. */
  onHijackChanged?: (workerId: string, enabled: boolean, owner?: string) => void | Promise<void>;
}

/** Options for {@link StateStore.notifyHijackChanged}. */
export interface HijackChangedOptions {
  /** Whether the worker is now hijacked. */
  enabled: boolean;
  /** Who holds it, when it is now hijacked. */
  owner?: string;
}

/** Whether `value` is a promise, matching the reference's isawaitable check. */
function isAwaitable(value: unknown): value is Promise<unknown> {
  return typeof (value as { then?: unknown } | null | undefined)?.then === "function";
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** Input buffering, worker lifecycle and the hijack-state predicates. */
export class StateStore {
  readonly #registry: WorkerRegistry<WorkerTermState>;
  readonly #maxBufferChars: number;
  readonly #now: () => number;
  readonly #logger: Logger;
  readonly #onMetric: ((name: string, value: number) => void) | undefined;
  readonly #onHijackChanged: ((workerId: string, enabled: boolean, owner?: string) => void | Promise<void>) | undefined;
  /** Partial input lines, keyed by the browser that is typing them. */
  readonly #buffers = new Map<Connection, string>();

  constructor(options: StateStoreOptions) {
    this.#registry = options.registry;
    this.#maxBufferChars = options.maxBufferChars;
    this.#now = options.now ?? monotonicNow;
    this.#logger = options.logger ?? noopLogger;
    this.#onMetric = options.onMetric;
    this.#onHijackChanged = options.onHijackChanged;
  }

  /**
   * Accumulate input for `ws`, returning the line once one is terminated.
   *
   * A buffer that would exceed the cap is *discarded whole* rather than
   * truncated or flushed — an over-long paste vanishes instead of arriving in
   * pieces, which bounds what one connection can make the hub hold. The cap is
   * checked before the newline scan, so an over-long write is dropped even
   * when it contains a terminator.
   */
  bufferAndGetCommand(ws: Connection, data: string): string | undefined {
    const buffer = (this.#buffers.get(ws) ?? "") + data;
    if (buffer.length > this.#maxBufferChars) {
      this.#buffers.delete(ws);
      return undefined;
    }
    if (buffer.includes("\r") || buffer.includes("\n")) {
      this.#buffers.delete(ws);
      return buffer;
    }
    this.#buffers.set(ws, buffer);
    return undefined;
  }

  /** What `ws` currently has buffered, for inspection. */
  bufferedFor(ws: Connection): string | undefined {
    return this.#buffers.get(ws);
  }

  /**
   * Forget a browser's partial line.
   *
   * Called when the connection closes: an abandoned buffer would otherwise be
   * retained for the lifetime of the hub.
   */
  dropBuffer(ws: Connection): void {
    this.#buffers.delete(ws);
  }

  /** The state for `workerId`, creating and registering it if it is new. */
  getOrCreate(workerId: string): WorkerTermState {
    const existing = this.#registry.get(workerId);
    if (existing !== undefined) {
      return existing;
    }
    const created = new WorkerTermState({ now: this.#now });
    this.#registry.put(workerId, created);
    return created;
  }

  /**
   * Stamp `workerId` as active now.
   *
   * A worker that has gone is ignored rather than recreated: it can disconnect
   * between a frame arriving and being handled, and resurrecting its state
   * here would leak an entry the pruner never sees a connection for.
   */
  touchActivity(workerId: string): void {
    const state = this.#registry.get(workerId);
    if (state !== undefined) {
      state.lastActivityAt = this.#now();
    }
  }

  /**
   * Emit a named metric, if a sink is configured.
   *
   * The value is truncated toward zero, matching the reference's `int()`
   * coercion. A throwing sink is swallowed and logged: observability must not
   * be able to tear down the session it is reporting on.
   */
  metric(name: string, value = 1): void {
    if (this.#onMetric === undefined) {
      return;
    }
    try {
      this.#onMetric(name, Math.trunc(value));
    } catch (error) {
      this.#logger.warn({ metric: name, error }, "metric_callback_failed");
    }
  }

  /** Whether `state` holds an unexpired REST hijack lease. */
  hasValidRestLease(state: WorkerTermState): boolean {
    const session = state.hijackSession;
    return session !== undefined && session.leaseExpiresAt > this.#now();
  }

  /**
   * Whether `state` holds an active dashboard hijack.
   *
   * An owner carrying *no* expiry counts as active — a perpetual hold. Note
   * that {@link WorkerTermState.lease}'s `isDashboardActive` answers `false`
   * for that same state. Both readings are in the reference and they are
   * genuinely inconsistent; unifying them would change who is allowed to send
   * input, so each keeps its own answer and the corpus pins both.
   */
  isDashboardHijackActive(state: WorkerTermState): boolean {
    if (state.hijackOwner === undefined) {
      return false;
    }
    if (state.hijackOwnerExpiresAt === undefined) {
      return true;
    }
    return state.hijackOwnerExpiresAt > this.#now();
  }

  /** Whether `state` is under any active hijack, dashboard or REST. */
  isHijacked(state: WorkerTermState): boolean {
    return this.isDashboardHijackActive(state) || this.hasValidRestLease(state);
  }

  /**
   * Tell the configured subscriber that a worker's hijack state changed.
   *
   * An async subscriber is fired and forgotten — the caller is mid-transition
   * and must not block on it — but its rejection is caught and logged, since
   * an unhandled one would take the process down under Node's default policy.
   *
   * A subscriber that throws *synchronously* propagates, deliberately unlike
   * {@link metric}: the reference leaves this call unguarded.
   */
  notifyHijackChanged(workerId: string, options: HijackChangedOptions): void {
    const callback = this.#onHijackChanged;
    if (callback === undefined) {
      return;
    }
    const result = callback(workerId, options.enabled, options.owner);
    // Awaitability, not non-undefined: a synchronous subscriber may still
    // return something (the reference asks inspect.isawaitable for the same
    // reason).
    if (isAwaitable(result)) {
      result.catch((error: unknown) => {
        this.#logger.warn({ workerId, error }, "on_hijack_changed callback raised");
      });
    }
  }
}
