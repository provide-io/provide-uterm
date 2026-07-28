//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { DIGIT_NOT_DECIMAL_RANGES, pyIsDigit } from "./index.ts";

interface PyStrGolden {
  unicode_version: string;
  isdigit: Array<{ name: string; text: string; result: boolean }>;
  decimal_ranges: Array<[number, number]>;
  digit_not_decimal_ranges: Array<[number, number]>;
  decimal_count: number;
  digit_not_decimal_count: number;
}

const golden = loadGolden<PyStrGolden>("pystr_golden.json");

describe("pyIsDigit", () => {
  it.each(golden.isdigit)("$name", (record) => {
    expect(pyIsDigit(record.text)).toBe(record.result);
  });

  it("agrees with CPython on every code point it accepts", () => {
    // Spot checks would miss a range; this walks the recorded ranges in full,
    // which is the only way to know the table is right rather than plausible.
    for (const [start, end] of [...golden.decimal_ranges, ...golden.digit_not_decimal_ranges]) {
      for (let cp = start; cp <= end; cp += 1) {
        expect(pyIsDigit(String.fromCodePoint(cp))).toBe(true);
      }
    }
  });

  it("rejects the code points immediately outside each accepted range", () => {
    // The off-by-one at a range edge is the failure this table invites.
    const accepted = new Set<number>();
    for (const [start, end] of [...golden.decimal_ranges, ...golden.digit_not_decimal_ranges]) {
      for (let cp = start; cp <= end; cp += 1) {
        accepted.add(cp);
      }
    }
    for (const [start, end] of [...golden.decimal_ranges, ...golden.digit_not_decimal_ranges]) {
      for (const edge of [start - 1, end + 1]) {
        if (edge >= 0 && !accepted.has(edge)) {
          expect(pyIsDigit(String.fromCodePoint(edge))).toBe(false);
        }
      }
    }
  });

  it("carries exactly the code points a JavaScript digit class misses", () => {
    // \p{Nd} already covers the decimal half; this table exists only for the
    // rest, and drifting it would silently narrow what counts as a digit.
    const carried = DIGIT_NOT_DECIMAL_RANGES.reduce((total, [start, end]) => total + (end - start + 1), 0);
    expect(carried).toBe(golden.digit_not_decimal_count);
    expect([...DIGIT_NOT_DECIMAL_RANGES]).toStrictEqual(golden.digit_not_decimal_ranges);
  });

  it("rejects a string that is only partly digits", () => {
    expect(pyIsDigit("1٣")).toBe(true);
    expect(pyIsDigit("1٣x")).toBe(false);
  });

  it("iterates by code point rather than by UTF-16 unit", () => {
    // A surrogate pair read as two units would test half a character.
    const astral = String.fromCodePoint(golden.digit_not_decimal_ranges.at(-1)?.[0] ?? 0);
    expect(astral.length).toBe(2);
    expect(pyIsDigit(astral)).toBe(true);
    expect(pyIsDigit(astral[0] ?? "")).toBe(false);
  });
});
