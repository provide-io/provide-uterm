//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * `splitIncompleteEscape` carries exactly what `stripAnsi` would strip once
 * complete. Mirrors the reference's `tests/terminal/test_split_incomplete_escape.py`.
 *
 * Both are built from the same grammar constants in `parse.ts`. The property
 * pinned here: for every sequence `stripAnsi` strips, cut at every position,
 * splitting the first part and carrying its tail into the second strips to
 * exactly what stripping the whole text does — and a cut strictly inside the
 * sequence carries all of its head.
 */

import { describe, expect, it } from "vitest";
import { ANSI_ESCAPE_SOURCE, MAX_ESCAPE_CARRY, splitIncompleteEscape, stripAnsi } from "./index.ts";

const PREFIX = "ab";
/**
 * Neither a CSI parameter, intermediate nor final byte, so the sequence under
 * test ends where it is meant to and nothing after it is pulled into it.
 */
const SUFFIX = "\n!";

/** Every two-byte (Fe) escape `stripAnsi` strips. */
function feSequences(): string[] {
  return Array.from({ length: 0x60 - 0x40 }, (_, index) => `\x1b${String.fromCharCode(0x40 + index)}`);
}

/** CSI sequences across parameter, intermediate and every final byte. */
function csiSequences(): string[] {
  const params = ["", "1", "38;2;255;0;0", "?25", "<=>", "0;1:2"];
  const intermediates = ["", " ", "!", "/ "];
  const finals = Array.from({ length: 0x7f - 0x40 }, (_, index) => String.fromCharCode(0x40 + index));
  return params.flatMap((p) => intermediates.flatMap((i) => finals.map((f) => `\x1b[${p}${i}${f}`)));
}

const SEQUENCES = [...feSequences(), ...csiSequences()];
const STRIPPED = new RegExp(`^(?:${ANSI_ESCAPE_SOURCE})$`);

describe("the carry and the stripper share one grammar", () => {
  it("carries every strippable sequence correctly, cut at every position", () => {
    expect(SEQUENCES).toHaveLength(32 + 6 * 4 * 63);
    for (const seq of SEQUENCES) {
      // A sequence stripAnsi strips.
      expect(STRIPPED.test(seq)).toBe(true);
      const expected = stripAnsi(PREFIX + seq + SUFFIX);
      expect(expected).toBe(PREFIX + SUFFIX);
      for (let cut = 0; cut <= seq.length; cut += 1) {
        const [complete, carry] = splitIncompleteEscape(PREFIX + seq.slice(0, cut));
        expect(complete + carry).toBe(PREFIX + seq.slice(0, cut));
        if (cut > 0 && cut < seq.length) {
          expect(carry).toBe(seq.slice(0, cut));
        }
        if (cut === 0) {
          expect(carry).toBe("");
        }
        expect(stripAnsi(complete) + stripAnsi(carry + seq.slice(cut) + SUFFIX)).toBe(expected);
      }
    }
  });

  it.each([
    ["", ["", ""]],
    ["plain", ["plain", ""]],
    ["a\x1b", ["a", "\x1b"]],
    ["a\x1b[38;2;255", ["a", "\x1b[38;2;255"]],
    ["a\x1b[1mb", ["a\x1b[1mb", ""]],
    ["a\x1bc", ["a\x1bc", ""]],
    ["\x1b[1m\x1b[3", ["\x1b[1m", "\x1b[3"]],
    [`\x1b[${"1".repeat(MAX_ESCAPE_CARRY - 2)}`, ["", `\x1b[${"1".repeat(MAX_ESCAPE_CARRY - 2)}`]],
    [`\x1b[${"1".repeat(MAX_ESCAPE_CARRY - 1)}`, [`\x1b[${"1".repeat(MAX_ESCAPE_CARRY - 1)}`, ""]],
    // Anchored at the end: a tail followed by a newline is not waiting for more.
    ["a\x1b[1\n", ["a\x1b[1\n", ""]],
  ])("splits %j as the reference does", (text, expected) => {
    expect(splitIncompleteEscape(text)).toStrictEqual(expected);
  });

  it("takes the carry bound as an argument, and defaults it to 64", () => {
    expect(MAX_ESCAPE_CARRY).toBe(64);
    expect(splitIncompleteEscape("x\x1b[12", 4)).toStrictEqual(["x", "\x1b[12"]);
    expect(splitIncompleteEscape("x\x1b[123", 4)).toStrictEqual(["x\x1b[123", ""]);
  });
});
