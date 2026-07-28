//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Keystroke timing, for the behavioural audit gate.
 *
 * Port of the Python module
 * `provide.uterm.server.bridge.hub.router_behavioral` and the Go package
 * `hub`.
 *
 * The gate can close a connection on the strength of these numbers, so they
 * are computed exactly as the reference computes them — including the sample
 * variance, which CPython evaluates rationally rather than in floating point.
 */

import { pyVariance } from "../pycompat/index.ts";
import { BoundedDeque, type Connection } from "./models.ts";

/**
 * How many keystroke timestamps are retained per browser.
 *
 * A rolling window rather than the whole session: an unbounded history would
 * converge on the session average and stop reacting to a change in rhythm.
 */
export const KEYSTROKE_RING_MAX = 50;

/** Typing-rhythm metrics for one browser. */
export interface KeystrokeHeuristics {
  /** Characters per second across the retained window. */
  cps: number;
  /** Sample variance of the inter-keystroke intervals. */
  jitter: number;
}

/** Construction options for {@link KeystrokeTracker}. */
export interface KeystrokeTrackerOptions {
  /** Monotonic clock in seconds. */
  now?: () => number;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** Nothing to measure yet, or nothing measurable. */
const NO_HEURISTICS: KeystrokeHeuristics = { cps: 0, jitter: 0 };

/** Per-browser keystroke timing. */
export class KeystrokeTracker {
  readonly #timestamps = new Map<Connection, BoundedDeque<number>>();
  readonly #now: () => number;

  constructor(options: KeystrokeTrackerOptions = {}) {
    this.#now = options.now ?? monotonicNow;
  }

  /** Note that `ws` sent a keystroke now. */
  record(ws: Connection): void {
    let ring = this.#timestamps.get(ws);
    if (ring === undefined) {
      ring = new BoundedDeque<number>(KEYSTROKE_RING_MAX);
      this.#timestamps.set(ws, ring);
    }
    ring.push(this.#now());
  }

  /**
   * Typing metrics for `ws`.
   *
   * Zeros mean "nothing to say" rather than "perfectly regular": fewer than
   * two keystrokes leaves no interval to measure, and a run that shares one
   * timestamp — what a paste looks like — implies a zero duration that would
   * otherwise divide by zero.
   */
  heuristics(ws: Connection): KeystrokeHeuristics {
    const ring = this.#timestamps.get(ws);
    // The length guard mirrors the reference; it is also what the zero
    // duration below would produce anyway, so it reads as intent rather than
    // as a behavioural fork.
    if (ring === undefined || ring.length < 2) {
      return { ...NO_HEURISTICS };
    }
    const samples = ring.toArray();
    const first = samples[0] as number;
    const last = samples[samples.length - 1] as number;
    const duration = last - first;
    const cps = duration > 0 ? (samples.length - 1) / duration : 0;

    const intervals: number[] = [];
    for (let index = 1; index < samples.length; index += 1) {
      intervals.push((samples[index] as number) - (samples[index - 1] as number));
    }
    // Sample variance, computed the way CPython does: exactly, then rounded
    // once. A float two-pass formula reports a non-zero jitter for a
    // perfectly even rhythm.
    const jitter = intervals.length > 1 ? pyVariance(intervals) : 0;
    return { cps, jitter };
  }

  /** Drop everything known about `ws`, which has disconnected. */
  forget(ws: Connection): void {
    this.#timestamps.delete(ws);
  }

  /** Which browsers are currently tracked, for inspection. */
  tracked(): Connection[] {
    return [...this.#timestamps.keys()];
  }
}
