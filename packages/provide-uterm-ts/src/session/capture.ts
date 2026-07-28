//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Bounded capture of what a session printed.
 *
 * Port of `TerminalCapture` in the Python module
 * `provide.uterm.transport_session`.
 */

/** Default bound on a capture, in characters. */
export const DEFAULT_CAPTURE_MAX_CHARS = 65_536;

/**
 * Bounded terminal text recorded for one caller-owned scope.
 *
 * The bound exists so a command producing megabytes cannot exhaust memory
 * merely because someone was watching. It keeps the *tail*: a caller
 * capturing output wants what the command finished saying, not the banner it
 * started with. That also makes the result a sliding window rather than a
 * prefix of the real output, which is worth knowing before comparing one
 * against the other.
 */
export class TerminalCapture {
  readonly #limit: number;
  /** Held as code points so the bound counts characters, not UTF-16 units. */
  #chars: string[] = [];

  constructor(maxChars: number = DEFAULT_CAPTURE_MAX_CHARS) {
    // A capture that can hold nothing would silently discard everything it
    // was asked to record.
    this.#limit = Math.max(1, Math.trunc(maxChars));
  }

  /** How many characters this capture will hold. */
  get limit(): number {
    return this.#limit;
  }

  /** What has been captured, oldest surviving character first. */
  get text(): string {
    return this.#chars.join("");
  }

  /**
   * Record a chunk, dropping the oldest characters if that overruns.
   *
   * Counting is by code point, matching CPython. Slicing by UTF-16 unit
   * instead would keep a different number of astral characters and could
   * leave a lone surrogate at the boundary — half an emoji, which corrupts
   * anything that re-encodes the text.
   */
  append(chunk: string): void {
    // Both guards below are faithful rather than load-bearing: spreading an
    // empty string pushes nothing, and slicing exactly `limit` characters to
    // the last `limit` is the identity. They mirror the reference, and no
    // test can distinguish them from their absence.
    if (chunk === "") {
      return;
    }
    this.#chars.push(...chunk);
    if (this.#chars.length > this.#limit) {
      this.#chars = this.#chars.slice(-this.#limit);
    }
  }
}
