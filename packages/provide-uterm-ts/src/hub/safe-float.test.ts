//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { safeFloat } from "./index.ts";

interface FloatGolden {
  default: number;
  values: Array<{ name: string; value: unknown; result: number | string }>;
  zero_default: number | string;
  negative_default: number | string;
}

const golden = loadGolden<FloatGolden>("safefloat_golden.json");

/** The corpus names the three values JSON cannot carry. */
function expected(result: number | string): number {
  if (result === "inf") {
    return Number.POSITIVE_INFINITY;
  }
  if (result === "-inf") {
    return Number.NEGATIVE_INFINITY;
  }
  return result === "nan" ? Number.NaN : (result as number);
}

describe("coercing a number off the wire", () => {
  it.each(golden.values)("$name", (record) => {
    expect(safeFloat(record.value, golden.default)).toBe(expected(record.result));
  });

  it("reads a number as itself", () => {
    expect(safeFloat(1.5, 2.5)).toBe(1.5);
    expect(safeFloat(0, 2.5)).toBe(0);
    expect(safeFloat(-1.5, 2.5)).toBe(-1.5);
  });

  it("falls back for anything absent", () => {
    // A caller that omitted a field wants whatever the server would have
    // used, not zero.
    expect(safeFloat(null, 2.5)).toBe(2.5);
    expect(safeFloat(undefined, 2.5)).toBe(2.5);
  });

  it("falls back for anything unreadable", () => {
    // Not distinguished from absent: a client sending nonsense wants the same
    // thing as one sending nothing.
    for (const value of ["nonsense", "", "   ", [1.5], { a: 1 }]) {
      expect(safeFloat(value, 2.5)).toBe(2.5);
    }
  });

  it("refuses an empty string where the host would read zero", () => {
    // `Number("")` is zero. A frame omitting a value would otherwise become a
    // timeout of zero rather than the configured one.
    expect(safeFloat("", 2.5)).toBe(2.5);
    expect(Number("")).toBe(0);
  });

  it("refuses a hexadecimal string where the host would read sixteen", () => {
    expect(safeFloat("0x10", 2.5)).toBe(2.5);
    expect(Number("0x10")).toBe(16);
  });

  it("takes the three names the host does not", () => {
    // Which a client can send, and which JSON cannot carry — so they arrive
    // as strings.
    expect(safeFloat("inf", 2.5)).toBe(Number.POSITIVE_INFINITY);
    expect(safeFloat("-inf", 2.5)).toBe(Number.NEGATIVE_INFINITY);
    expect(safeFloat("nan", 2.5)).toBeNaN();
    expect(Number("inf")).toBeNaN();
  });

  it("reads a name in any case", () => {
    expect(safeFloat("INFINITY", 2.5)).toBe(Number.POSITIVE_INFINITY);
  });

  it("accepts the spacing and separators a person writes", () => {
    expect(safeFloat("  1.5  ", 2.5)).toBe(1.5);
    expect(safeFloat("1_000.5", 2.5)).toBe(1000.5);
    expect(safeFloat("+1.5", 2.5)).toBe(1.5);
    expect(safeFloat("1e3", 2.5)).toBe(1000);
  });

  it("reads a boolean as a number", () => {
    // Which the reference does, and which a client sending `true` for a rate
    // therefore gets.
    expect(safeFloat(true, 2.5)).toBe(1);
    expect(safeFloat(false, 2.5)).toBe(0);
  });

  it("returns the fallback it was given, whatever it is", () => {
    expect(safeFloat("nonsense", 0)).toBe(expected(golden.zero_default));
    expect(safeFloat(null, -1)).toBe(expected(golden.negative_default));
  });
});
