//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Making an automated worker pausable by a human operator.
 *
 * Port of the Python module `provide.uterm.bridge.base`.
 *
 * A worker calls {@link Hijackable.awaitIfHijacked} at each checkpoint in its
 * loop. Normally that returns at once; while an operator holds the session it
 * blocks, and the dashboard can let the loop through one iteration at a time.
 *
 * The reference is a mixin because Python has cooperative multiple
 * inheritance. Here it is a plain object a worker holds, which is the same
 * capability without the diamond.
 */

/**
 * Most step tokens that can be banked at once.
 *
 * Unbounded accumulation from a client hammering the step button would let
 * the loop run away the moment the hijack is released.
 */
export const STEP_TOKEN_CAP = 100;

/**
 * Checkpoints one press of Step buys.
 *
 * Two — plan and act — so a single press advances the loop exactly one
 * iteration rather than half of one.
 */
export const STEP_TOKENS_PER_ITERATION = 2;

/** Default seconds without progress before the watchdog fires. */
const DEFAULT_STUCK_TIMEOUT_S = 120;

/** Default seconds between watchdog checks. */
const DEFAULT_CHECK_INTERVAL_S = 5;

/** Shortest check interval; below this the loop spins rather than watches. */
const MIN_CHECK_INTERVAL_S = 0.5;

/** Options for {@link Hijackable}. */
export interface HijackableOptions {
  /** Monotonic clock in seconds. */
  now?: () => number;
  /** How the watchdog waits between checks. */
  sleep?: (seconds: number) => Promise<void>;
}

/** Options for {@link Hijackable.startWatchdog}. */
export interface WatchdogOptions {
  /** Seconds without progress before firing. */
  stuckTimeoutS?: number;
  /** Seconds between checks, floored at {@link MIN_CHECK_INTERVAL_S}. */
  checkIntervalS?: number;
  /** Called when the worker looks stuck. Typically drops the session. */
  onStuck?: () => Promise<void>;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** Makes an automated worker pausable by a human operator. */
export class Hijackable {
  readonly #now: () => number;
  readonly #sleep: (seconds: number) => Promise<void>;
  #hijacked = false;
  #stepTokens = 0;
  #lastProgress: number;
  /** Resolvers for checkpoints currently blocked. */
  #waiters: Array<() => void> = [];
  #watchdog: { stop: () => void; done: Promise<void> } | undefined;

  constructor(options: HijackableOptions = {}) {
    this.#now = options.now ?? monotonicNow;
    this.#sleep = options.sleep ?? ((seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000)));
    this.#lastProgress = this.#now();
  }

  /** Whether an operator currently holds the worker. */
  get hijacked(): boolean {
    return this.#hijacked;
  }

  /** Checkpoints the worker may still pass while held. */
  get stepTokens(): number {
    return this.#stepTokens;
  }

  /**
   * A checkpoint in the worker's loop.
   *
   * Returns at once when nobody holds the worker, spends a step token when
   * one is banked, and otherwise blocks until the hijack ends — which is the
   * whole point: the automation stops where the operator put it.
   */
  async awaitIfHijacked(): Promise<void> {
    if (!this.#hijacked) {
      return;
    }
    if (this.#stepTokens > 0) {
      this.#stepTokens -= 1;
      return;
    }
    await new Promise<void>((resolve) => {
      this.#waiters.push(resolve);
    });
  }

  /**
   * Pause or resume the worker. Idempotent.
   *
   * Taking the worker discards any tokens granted for a previous hold, so an
   * operator pausing again does not find the loop already walking.
   */
  async setHijacked(enabled: boolean): Promise<void> {
    if (enabled === this.#hijacked) {
      return;
    }
    this.#hijacked = enabled;
    if (enabled) {
      this.#stepTokens = 0;
      return;
    }
    const waiters = this.#waiters;
    this.#waiters = [];
    for (const resolve of waiters) {
      resolve();
    }
  }

  /**
   * Let the worker through `checkpoints` more gates while still held.
   *
   * A no-op when nobody holds it: banking tokens beforehand would let a later
   * hijack be walked straight through. A negative request adds nothing rather
   * than debiting credit already granted, which would otherwise let a client
   * drive the count below zero and unblock the gate for good.
   */
  async requestStep(checkpoints: number = STEP_TOKENS_PER_ITERATION): Promise<void> {
    if (!this.#hijacked) {
      return;
    }
    const requested = Math.max(0, Math.trunc(checkpoints));
    this.#stepTokens = Math.min(this.#stepTokens + requested, STEP_TOKEN_CAP);
  }

  /** Note that the worker did something, resetting the watchdog. */
  noteProgress(): void {
    this.#lastProgress = this.#now();
  }

  /**
   * Watch for the worker going quiet, and call `onStuck` when it does.
   *
   * Suppressed while the worker is held: a paused worker is not a stuck one,
   * and firing then would drop the very session the operator is driving.
   *
   * The timer resets after firing, so a slow reconnect is not spammed with
   * repeat callbacks. A second start is ignored while one is already running.
   */
  startWatchdog(options: WatchdogOptions = {}): void {
    if (this.#watchdog !== undefined) {
      return;
    }
    const stuckTimeoutS = options.stuckTimeoutS ?? DEFAULT_STUCK_TIMEOUT_S;
    const intervalS = Math.max(MIN_CHECK_INTERVAL_S, options.checkIntervalS ?? DEFAULT_CHECK_INTERVAL_S);
    let running = true;

    const done = (async () => {
      while (running) {
        await this.#sleep(intervalS);
        if (!running) {
          return;
        }
        if (this.#hijacked) {
          // Held, not stalled — keep the clock fresh so releasing the hold
          // does not immediately look like a stall.
          this.noteProgress();
          continue;
        }
        if (this.#now() - this.#lastProgress < stuckTimeoutS) {
          continue;
        }
        if (options.onStuck !== undefined) {
          try {
            await options.onStuck();
          } catch {
            // The callback is recovery code; if it fails there is nothing
            // better to do than keep watching.
          }
        }
        this.noteProgress();
      }
    })();

    this.#watchdog = {
      stop: () => {
        running = false;
      },
      done,
    };
  }

  /** Stop the watchdog. Idempotent. */
  async stopWatchdog(): Promise<void> {
    const watchdog = this.#watchdog;
    if (watchdog === undefined) {
      return;
    }
    this.#watchdog = undefined;
    watchdog.stop();
  }

  /**
   * Release the worker and stop watching it.
   *
   * Called from the worker's shutdown, so neither a paused loop nor a live
   * timer is left behind.
   */
  async cleanupHijack(): Promise<void> {
    await this.setHijacked(false);
    await this.stopWatchdog();
  }
}
