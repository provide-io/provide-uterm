//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { pyRoundTo } from "./rounding.ts";

interface RoundGolden {
  cases: Array<{ name: string; value_hex: string; value: number; ndigits: number; rounded: number }>;
}

const golden = loadGolden<RoundGolden>("pyround_golden.json");

describe("rounding to a number of places", () => {
  it.each(golden.cases)("$name", (record) => {
    expect(pyRoundTo(record.value, record.ndigits)).toBe(record.rounded);
  });

  it("breaks ties towards the even neighbour", () => {
    // Math.round and toFixed both go away from zero and would give 0.0313.
    expect(pyRoundTo(0.03125, 4)).toBe(0.0312);
    expect(pyRoundTo(0.40625, 4)).toBe(0.4062);
    expect(pyRoundTo(0.5, 0)).toBe(0);
    expect(pyRoundTo(1.5, 0)).toBe(2);
    expect(pyRoundTo(2.5, 0)).toBe(2);
  });

  it("tests the tie on the value it was given, not on a scaled copy", () => {
    // One ulp either side of an exact tie. Scaling first collapses all three
    // onto the same product, so a port that multiplies before deciding gets
    // the neighbours wrong in one direction or the other.
    const low = golden.cases.find((entry) => entry.name === "a tie one ulp low");
    const high = golden.cases.find((entry) => entry.name === "a tie one ulp high");
    expect(low?.rounded).toBe(0.0312);
    expect(high?.rounded).toBe(0.0313);
    expect(pyRoundTo(low?.value as number, 4)).toBe(0.0312);
    expect(pyRoundTo(high?.value as number, 4)).toBe(0.0313);
  });

  it("rounds a decimal literal by what it actually stores", () => {
    // 2.675 is held just below the tie, so it rounds down however much the
    // written form looks like it should go up.
    expect(pyRoundTo(2.675, 2)).toBe(2.67);
    expect(pyRoundTo(-2.675, 2)).toBe(-2.67);
  });

  it("is symmetric about zero", () => {
    for (const record of golden.cases) {
      expect(pyRoundTo(-record.value, record.ndigits)).toBe(-record.rounded);
    }
  });

  it("scales a value that has no fraction left", () => {
    // Past 2**52 a double is a whole number, which takes the other branch —
    // and that branch still has to divide by the scale on the way out.
    expect(pyRoundTo(2 ** 60, 4)).toBe(2 ** 60);
    expect(pyRoundTo(1e300, 4)).toBe(1e300);
    expect(pyRoundTo(-(2 ** 60), 4)).toBe(-(2 ** 60));
  });

  it("reads a subnormal's exponent as pinned rather than derived", () => {
    // A subnormal carries no implicit leading one and sits one exponent above
    // where the stored zero would put it.
    expect(pyRoundTo(5e-324, 4)).toBe(0);
    expect(pyRoundTo(1e-320, 4)).toBe(0);
    expect(pyRoundTo(5e-324, 400)).toBe(5e-324);
    expect(pyRoundTo(1e-320, 330)).toBe(1e-320);
  });

  it("leaves a value that is already short alone", () => {
    expect(pyRoundTo(0.5, 4)).toBe(0.5);
    expect(pyRoundTo(42, 4)).toBe(42);
    expect(pyRoundTo(0, 4)).toBe(0);
  });

  it("passes a value that cannot be rounded straight through", () => {
    // A NaN or an infinity has no decimal expansion to quantise.
    expect(pyRoundTo(Number.NaN, 4)).toBeNaN();
    expect(pyRoundTo(Number.POSITIVE_INFINITY, 4)).toBe(Number.POSITIVE_INFINITY);
    expect(pyRoundTo(Number.NEGATIVE_INFINITY, 4)).toBe(Number.NEGATIVE_INFINITY);
  });
});
