//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Screen buffering with timing metadata for prompt detection.
 *
 * Port of the Python module `provide.uterm.detection.buffer` and the Go
 * package `detection`.
 */

/**
 * A screen snapshot, as the emulator hands one over.
 *
 * The reference declares this once, in `detection.models`, and both the
 * buffer and the detector read it: the screen and its hash are required, and
 * the cursor metadata is whatever the producer could work out. The detector
 * takes a `Partial` of it, because it provably survives a snapshot that is
 * missing the required fields and turning that into a crash mid-frame would
 * end a session over a malformed frame.
 */
export interface ScreenSnapshot {
  screen: string;
  screen_hash: string;
  /** Capture time in seconds. Defaults to now when absent. */
  captured_at?: number;
  /** Whether the cursor sits after the last drawn character. */
  cursor_at_end?: boolean;
  /** Whether the screen ends in a space, which suggests a live input field. */
  has_trailing_space?: boolean;
  /** Where the cursor is, as far as the emulator knows. */
  cursor?: { x?: unknown; y?: unknown } | null;
  [key: string]: unknown;
}

/** A buffered screen snapshot with timing metadata. */
export interface ScreenBuffer {
  screen: string;
  screen_hash: string;
  snapshot: ScreenSnapshot;
  captured_at: number;
  matched_prompt_id: string | null;
  /** Seconds since the screen last changed, or 0 before the first change. */
  time_since_last_change: number;
}

/** Manages a bounded screen history with timing calculation. */
export class BufferManager {
  readonly #maxSize: number;
  #buffer: ScreenBuffer[] = [];
  #lastHash = "";
  #lastChangeTime = 0;

  constructor(maxSize = 50) {
    this.#maxSize = maxSize;
  }

  /** How many screens are held. */
  get size(): number {
    return this.#buffer.length;
  }

  /** The most this will hold before dropping the oldest. */
  get maxSize(): number {
    return this.#maxSize;
  }

  /**
   * Add a screen snapshot and compute its timing metadata.
   *
   * The elapsed time is measured from the last *change*, not the last
   * snapshot, so a screen that keeps arriving unchanged reports a growing
   * idle interval. It stays zero until a first change has been seen, which
   * is what keeps a freshly-started session from looking idle.
   */
  addScreen(snapshot: ScreenSnapshot): ScreenBuffer {
    const now = snapshot.captured_at ?? Date.now() / 1000;
    const screenHash = snapshot.screen_hash;
    const elapsed = this.#lastChangeTime > 0 ? now - this.#lastChangeTime : 0;

    if (screenHash !== this.#lastHash) {
      this.#lastHash = screenHash;
      this.#lastChangeTime = now;
    }

    const buffer: ScreenBuffer = {
      screen: snapshot.screen,
      screen_hash: screenHash,
      snapshot,
      captured_at: now,
      matched_prompt_id: null,
      time_since_last_change: elapsed,
    };
    this.#buffer.push(buffer);
    if (this.#buffer.length > this.#maxSize) {
      this.#buffer = this.#buffer.slice(-this.#maxSize);
    }
    return buffer;
  }

  /** The `n` most recent buffered screens, oldest first. */
  getRecent(n = 5): ScreenBuffer[] {
    if (n >= this.#buffer.length) {
      return [...this.#buffer];
    }
    return this.#buffer.slice(-n);
  }

  /**
   * Whether the screen has been unchanged for at least `thresholdSeconds`.
   *
   * Measured against the wall clock rather than the last snapshot, so a
   * session that has stopped producing output goes idle without needing
   * another snapshot to arrive and say so.
   */
  detectIdleState(thresholdSeconds = 2): boolean {
    if (this.#lastChangeTime === 0 || this.#lastHash === "") {
      return false;
    }
    return Date.now() / 1000 - this.#lastChangeTime >= thresholdSeconds;
  }

  /** Clear the buffer and reset the change clock. */
  clear(): void {
    this.#buffer = [];
    this.#lastHash = "";
    this.#lastChangeTime = 0;
  }
}
