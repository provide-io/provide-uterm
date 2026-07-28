//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Catching a pattern split across consecutive chunks.
 *
 * Port of the Python module `provide.uterm.annotation._streaming`.
 *
 * {@link PatternDetector} is stateless and scans one chunk at a time, so a
 * secret that happens to straddle two reads is silently missed. This carries a
 * bounded tail of the previous chunk into the next one.
 *
 * Stateful: one instance per logical stream, and not shared across event types
 * whose text must not be concatenated. The detector it wraps stays stateless
 * and may be shared.
 */

import type { PatternDetector } from "./detector.ts";
import type { Annotation } from "./models.ts";

/**
 * How much of a chunk is carried.
 *
 * The longest fixed-shape secret expected to bridge a boundary, which bounds
 * both the memory held and the text re-scanned.
 */
export const DEFAULT_MAX_CARRY = 512;

/** Bridges chunk boundaries for one stream. */
export class StreamingDetector {
  readonly #detector: PatternDetector;
  readonly #maxCarry: number;
  #carry = "";

  constructor(detector: PatternDetector, options: { maxCarry?: number } = {}) {
    this.#detector = detector;
    this.#maxCarry = options.maxCarry ?? DEFAULT_MAX_CARRY;
  }

  /**
   * Scan `text` joined to the carried tail.
   *
   * A match belongs to the chunk it *completes* in, so the span carries that
   * chunk's sequence number. The tail kept is the window after the furthest
   * match: it bridges a secret straddling the next boundary — including one
   * beginning immediately after a completed match — without re-reporting a
   * match that already finished.
   */
  detect(eventType: string, text: string, seq: number): Annotation[] {
    // An empty read happens. Skipping it keeps the carry intact — which is
    // also what re-scanning it would do, since the carry never holds a
    // completed match — but it says that an empty chunk is not an event.
    if (text === "") {
      return [];
    }
    const window = this.#carry === "" ? text : this.#carry + text;
    const { annotations, matchEnd } = this.#detector.scan(eventType, window, seq);
    this.#carry = window.slice(matchEnd).slice(-this.#maxCarry);
    return annotations;
  }

  /** Forget the carried tail, on a screen clear or a resync. */
  reset(): void {
    this.#carry = "";
  }
}
