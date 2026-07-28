//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Snapshot polling for the hijack REST endpoints.
 *
 * Port of the Python module
 * `provide.uterm.server.bridge.hub.polling_service` and the Go package `hub`
 * (`polling.go`).
 *
 * A REST caller sends keystrokes and then waits for the screen to look a
 * certain way. The worker pushes snapshots on its own schedule, so waiting
 * means polling — and the interesting behaviour is when the worker gets
 * nudged for a fresh one, which is deliberately *not* on every poll.
 */

import type { WorkerTermState } from "./models.ts";
import type { WorkerRegistry } from "./registry.ts";
import { compileExpectRegex, PromptRegexError, snapshotMatches } from "./rest-helpers.ts";

/** Default wait for a fresh snapshot, in milliseconds. */
const DEFAULT_SNAPSHOT_TIMEOUT_MS = 1500;

/** How often {@link PollingCoordinator.waitForSnapshot} re-reads, in seconds. */
const SNAPSHOT_POLL_INTERVAL_S = 0.08;

/** Shortest guard timeout that is honoured, in milliseconds. */
const MIN_GUARD_TIMEOUT_MS = 50;

/** Shortest guard poll interval that is honoured, in milliseconds. */
const MIN_GUARD_INTERVAL_MS = 20;

/** The hub surface the polling coordinator reaches back through. */
export interface PollingHubCallbacks {
  /** The worker table snapshots are read from. */
  registry: WorkerRegistry<WorkerTermState>;
  /** Ask the worker for a fresh snapshot. */
  requestSnapshot(workerId: string): Promise<void>;
  /** Monotonic seconds, for the deadline. */
  monotonic(): number;
  /** Wait between polls. */
  sleep(seconds: number): Promise<void>;
}

/** Construction options for {@link PollingCoordinator}. */
export interface PollingCoordinatorOptions {
  /** Registry access, worker pokes and the clock, injected as one surface. */
  hub: PollingHubCallbacks;
  /** Wall clock in seconds, compared against worker snapshot timestamps. */
  wallNow?: () => number;
  /**
   * Guard compiler. Defaults to {@link compileExpectRegex}.
   *
   * A seam rather than a knob: it keeps the refusal path testable, and leaves
   * room for a caller that accepts a different guard dialect. Anything it
   * throws that is not a {@link PromptRegexError} is a bug rather than a bad
   * pattern, and is rethrown rather than reported as a refusal.
   */
  compileGuard?: (pattern?: string) => RegExp | undefined;
}

/** Arguments for {@link PollingCoordinator.waitForGuard}. */
export interface GuardWaitOptions {
  /** Wait until the detected prompt has this id. */
  expectPromptId?: string;
  /** Wait until the screen matches this pattern. */
  expectRegex?: string;
  /** How long to wait, floored at {@link MIN_GUARD_TIMEOUT_MS}. */
  timeoutMs: number;
  /** How often to re-read, floored at {@link MIN_GUARD_INTERVAL_MS}. */
  pollIntervalMs: number;
}

/** The outcome of a guarded wait. */
export interface GuardWaitResult {
  /** Whether the guard was satisfied. */
  matched: boolean;
  /** The last snapshot seen, satisfying or not. */
  snapshot?: Record<string, unknown> | undefined;
  /** Why it failed, when it did. */
  reason?: string;
}

/** Snapshot polling for the hijack REST endpoints. */
export class PollingCoordinator {
  readonly #hub: PollingHubCallbacks;
  readonly #wallNow: () => number;
  readonly #compileGuard: (pattern?: string) => RegExp | undefined;

  constructor(options: PollingCoordinatorOptions) {
    this.#hub = options.hub;
    this.#wallNow = options.wallNow ?? (() => Date.now() / 1000);
    this.#compileGuard = options.compileGuard ?? ((pattern) => compileExpectRegex(pattern));
  }

  /**
   * Wait for a snapshot taken *after* this call.
   *
   * The worker has almost always got an older snapshot sitting there, and
   * returning it would answer a question the caller did not ask — so the
   * request timestamp is the floor. It is wall time, because that is the
   * clock the worker stamps its snapshots with.
   */
  async waitForSnapshot(
    workerId: string,
    timeoutMs: number = DEFAULT_SNAPSHOT_TIMEOUT_MS,
  ): Promise<Record<string, unknown> | undefined> {
    const requestedAt = this.#wallNow();
    const deadline = this.#hub.monotonic() + timeoutMs / 1000;
    await this.#hub.requestSnapshot(workerId);
    while (this.#hub.monotonic() < deadline) {
      const state = this.#hub.registry.get(workerId);
      if (state === undefined) {
        // The worker has gone; sleeping out the timeout would only delay
        // telling the caller so.
        return undefined;
      }
      const snapshot = state.lastSnapshot;
      if (snapshot !== undefined && Number(snapshot.ts ?? 0) > requestedAt) {
        return snapshot;
      }
      await this.#hub.sleep(SNAPSHOT_POLL_INTERVAL_S);
    }
    return undefined;
  }

  /**
   * Wait until a snapshot satisfies the given guards.
   *
   * A guard that will not compile comes back as a `reason` rather than a
   * timeout: the caller needs to know their pattern was refused, not that the
   * screen never matched it. Nothing is polled and the worker is not touched.
   *
   * With no guards at all there is nothing to wait for, so the current
   * snapshot is returned immediately — the worker is still nudged, so the
   * *next* caller finds something fresh.
   *
   * The worker is re-nudged only when the snapshot timestamp has not moved
   * since the previous poll. A worker already streaming snapshots would
   * otherwise be flooded with requests it is already answering.
   */
  async waitForGuard(workerId: string, options: GuardWaitOptions): Promise<GuardWaitResult> {
    let expectRegex: RegExp | undefined;
    try {
      expectRegex = this.#compileGuard(options.expectRegex);
    } catch (error) {
      if (error instanceof PromptRegexError) {
        return { matched: false, snapshot: undefined, reason: error.message };
      }
      throw error;
    }

    const hasPromptGuard = options.expectPromptId !== undefined && options.expectPromptId !== "";
    if (!hasPromptGuard && expectRegex === undefined) {
      const snapshot = this.#hub.registry.get(workerId)?.lastSnapshot;
      await this.#hub.requestSnapshot(workerId);
      return { matched: true, snapshot };
    }

    const deadline = this.#hub.monotonic() + Math.max(MIN_GUARD_TIMEOUT_MS, options.timeoutMs) / 1000;
    const interval = Math.max(MIN_GUARD_INTERVAL_MS, options.pollIntervalMs) / 1000;
    let snapshot: Record<string, unknown> | undefined;
    let lastSnapshotTs = 0;

    await this.#hub.requestSnapshot(workerId);
    while (this.#hub.monotonic() < deadline) {
      snapshot = this.#hub.registry.get(workerId)?.lastSnapshot;
      if (snapshotMatches(snapshot, { expectPromptId: options.expectPromptId, expectRegex })) {
        return { matched: true, snapshot };
      }
      const snapshotTs = snapshot === undefined ? 0 : Number(snapshot.ts ?? 0);
      if (snapshotTs <= lastSnapshotTs) {
        await this.#hub.requestSnapshot(workerId);
      }
      lastSnapshotTs = snapshotTs;
      await this.#hub.sleep(interval);
    }
    return { matched: false, snapshot, reason: "prompt_guard_not_satisfied" };
  }
}
