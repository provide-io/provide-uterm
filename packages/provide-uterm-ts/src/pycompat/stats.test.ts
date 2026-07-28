//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { exactRatio, pyVariance, ratioToNumber } from "./index.ts";

interface PyStatsGolden {
  variances: Array<{ name: string; data: number[]; variance: number; naive_agrees: boolean }>;
  ratios: Array<{ name: string; value: number; numerator: string; denominator: string }>;
}

const golden = loadGolden<PyStatsGolden>("pystats_golden.json");

/** The two-pass float formula, for contrast only. */
function naiveVariance(data: number[]): number {
  const n = data.length;
  const mean = data.reduce((total, x) => total + x, 0) / n;
  return data.reduce((total, x) => total + (x - mean) ** 2, 0) / (n - 1);
}

describe("pyVariance", () => {
  it.each(golden.variances)("$name", (record) => {
    // Exact to the last bit, not close: CPython accumulates rationally and
    // rounds once, and the port's contract is the same value, not a similar
    // one.
    expect(pyVariance(record.data)).toBe(record.variance);
  });

  it("differs from the two-pass float formula where CPython does", () => {
    // The cases that make this worth implementing at all. A port checked only
    // against well-conditioned data would look correct and be wrong here.
    const differing = golden.variances.filter((record) => !record.naive_agrees);
    expect(differing.length).toBeGreaterThan(0);
    for (const record of differing) {
      expect(naiveVariance(record.data)).not.toBe(record.variance);
      expect(pyVariance(record.data)).toBe(record.variance);
    }
  });

  it("reports exactly zero for identical values", () => {
    // The naive formula does not: the mean carries rounding error, and the
    // squared deviations of identical inputs come out non-zero.
    const record = golden.variances.find((entry) => entry.name === "tenths");
    expect(record?.variance).toBe(0);
    expect(naiveVariance([0.1, 0.1, 0.1])).not.toBe(0);
    expect(pyVariance([0.1, 0.1, 0.1])).toBe(0);
  });

  it("uses the sample denominator", () => {
    // n-1, not n. The population formula would give 0.25 here.
    expect(pyVariance([1, 2])).toBe(0.5);
  });

  it("refuses fewer than two values", () => {
    // CPython raises StatisticsError; there is no meaningful sample variance
    // of one point, and returning zero would look like perfect regularity.
    expect(() => pyVariance([])).toThrow(RangeError);
    expect(() => pyVariance([1])).toThrow(RangeError);
  });
});

describe("exactRatio", () => {
  it.each(golden.ratios)("decomposes $name", (record) => {
    // Compared against CPython's own as_integer_ratio, digit for digit. The
    // subnormal cases carry no implicit leading mantissa bit and sit at the
    // smallest exponent rather than one below it, which is a separate arm a
    // port is easy to get wrong and unlikely to notice.
    const [numerator, denominator] = exactRatio(record.value);
    expect(numerator).toBe(BigInt(record.numerator));
    expect(denominator).toBe(BigInt(record.denominator));
  });

  it("round-trips every normal value", () => {
    for (const record of golden.ratios) {
      if (record.value !== 0 && Math.abs(record.value) < Number.MIN_VALUE * 2 ** 52) {
        continue;
      }
      const [numerator, denominator] = exactRatio(record.value);
      expect(ratioToNumber(numerator, denominator)).toBe(record.value);
    }
  });

  it("gives a power-of-two denominator", () => {
    // Every finite double is an integer over a power of two, which is what
    // makes the exact accumulation cheap.
    const [, denominator] = exactRatio(0.1);
    expect(denominator & (denominator - 1n)).toBe(0n);
  });

  it("rejects a value that is not finite", () => {
    expect(() => exactRatio(Number.NaN)).toThrow(RangeError);
    expect(() => exactRatio(Number.POSITIVE_INFINITY)).toThrow(RangeError);
  });
});

describe("ratioToNumber", () => {
  it("rounds a tie to the even neighbour", () => {
    // 2^53 + 1 is exactly halfway between 2^53 and 2^53 + 2, and 2^53 + 3 is
    // exactly halfway between 2^53 + 2 and 2^53 + 4. IEEE 754 picks the
    // neighbour with an even mantissa, which is down in the first case and
    // up in the second — so neither "always down" nor "always up" passes.
    const base = 2n ** 53n;
    expect(ratioToNumber(base + 1n, 1n)).toBe(2 ** 53);
    expect(ratioToNumber(base + 3n, 1n)).toBe(2 ** 53 + 4);
  });

  it("rounds a non-tie to the nearer neighbour", () => {
    // Above 2^53 the spacing is 2, so the halfway point is +1: a value at
    // +1.5 rounds up and one at +0.75 rounds down, whatever the tie rule is.
    const base = 2n ** 53n;
    expect(ratioToNumber(base * 2n + 3n, 2n)).toBe(2 ** 53 + 2);
    expect(ratioToNumber(base * 4n + 3n, 4n)).toBe(2 ** 53);
  });

  it("handles zero and negatives", () => {
    expect(ratioToNumber(0n, 7n)).toBe(0);
    expect(ratioToNumber(-1n, 2n)).toBe(-0.5);
    expect(ratioToNumber(1n, -2n)).toBe(-0.5);
    expect(ratioToNumber(-1n, -2n)).toBe(0.5);
  });

  it("survives magnitudes a Number conversion could not", () => {
    // Converting numerator and denominator separately would overflow to
    // Infinity and yield NaN; the division has to happen in exact integers.
    const huge = 10n ** 400n;
    expect(ratioToNumber(huge * 3n, huge * 2n)).toBe(1.5);
  });

  it("handles a numerator far larger than its denominator", () => {
    // The quotient starts far wider than the mantissa and has to be scaled
    // down rather than up.
    expect(ratioToNumber(2n ** 200n, 1n)).toBe(2 ** 200);
    expect(ratioToNumber(2n ** 200n * 3n, 2n)).toBe(1.5 * 2 ** 200);
  });

  it("carries correctly when rounding overflows the mantissa", () => {
    // Rounding up a quotient of all ones widens it by a bit, so the exponent
    // has to absorb the carry.
    const justUnder = 2n ** 53n - 1n;
    expect(ratioToNumber(justUnder * 4n + 3n, 4n)).toBe(2 ** 53);
  });

  it("rejects a zero denominator", () => {
    expect(() => ratioToNumber(1n, 0n)).toThrow(RangeError);
  });
});
