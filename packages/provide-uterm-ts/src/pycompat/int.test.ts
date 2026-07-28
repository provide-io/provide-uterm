//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { pyInt, safeInt } from "./index.ts";

interface WorkerLinkGolden {
  safe_ints: Array<{
    name: string;
    value: unknown;
    default: number;
    min_val: number | null;
    result: number;
  }>;
}

const golden = loadGolden<WorkerLinkGolden>("worker_link_golden.json");

describe("safeInt", () => {
  it.each(golden.safe_ints)("$name", (record) => {
    // These values arrive off the wire and end up in a PTY ioctl, so the
    // coercion is a boundary check, not a convenience.
    expect(safeInt(record.value, record.default, record.min_val === null ? {} : { minVal: record.min_val })).toBe(
      record.result,
    );
  });

  it("falls back rather than throwing on anything unparseable", () => {
    for (const value of [Number.NaN, Number.POSITIVE_INFINITY, {}, [], "abc", null, undefined]) {
      expect(safeInt(value, 42)).toBe(42);
    }
  });

  it("falls back when the value is below the floor", () => {
    // Not clamped to the floor — the default is used instead, because a
    // caller sending zero columns meant something other than one column.
    expect(safeInt(0, 25, { minVal: 1 })).toBe(25);
    expect(safeInt(-5, 25, { minVal: 1 })).toBe(25);
    expect(safeInt(1, 25, { minVal: 1 })).toBe(1);
  });

  it("applies no floor when none is given", () => {
    expect(safeInt(-5, 25)).toBe(-5);
  });

  it("coerces the default only for an absent value", () => {
    // An asymmetry in the reference, not a choice here: a missing value is
    // coerced through int(default), while every other rejection returns the
    // default untouched. A fractional default shows the difference.
    expect(safeInt(null, 25.7)).toBe(25);
    expect(safeInt("bad", 25.7)).toBe(25.7);
  });
});

describe("pyInt", () => {
  it("truncates toward zero", () => {
    expect(pyInt(40.9)).toBe(40);
    expect(pyInt(-40.9)).toBe(-40);
  });

  it("accepts a numeric string with surrounding whitespace", () => {
    expect(pyInt(" 123 ")).toBe(123);
    expect(pyInt("\n12\t")).toBe(12);
  });

  it("accepts underscore separators between digits", () => {
    // CPython's literal syntax reaches int() too, which is surprising until
    // a config value like 1_000 arrives and a stricter parser refuses it.
    expect(pyInt("1_0")).toBe(10);
    expect(pyInt("1_000_000")).toBe(1_000_000);
  });

  it("refuses a misplaced underscore", () => {
    for (const text of ["_10", "10_", "1__0", "_"]) {
      expect(pyInt(text)).toBeUndefined();
    }
  });

  it("accepts a sign", () => {
    expect(pyInt("+7")).toBe(7);
    expect(pyInt("-7")).toBe(-7);
    expect(pyInt("+ 7")).toBeUndefined();
  });

  it("accepts Unicode decimal digits but not the wider digit set", () => {
    // int() takes Nd only. A superscript passes str.isdigit and is still
    // refused here, which is exactly the pair that catches a port reusing
    // one predicate for both.
    expect(pyInt("٣")).toBe(3);
    expect(pyInt("²")).toBeUndefined();
  });

  it("maps every digit of a non-ASCII run", () => {
    // The run is walked back to its zero, so the last digit needs the walk
    // to run its full length rather than falling out early.
    expect(pyInt("٠١٢٣٤٥٦٧٨٩")).toBe(123_456_789);
    expect(pyInt("٩")).toBe(9);
  });

  it("refuses a float-looking string", () => {
    // "1.5" is a float literal, not an integer one; CPython refuses it and a
    // port that used parseFloat would silently accept and truncate it.
    expect(pyInt("1.5")).toBeUndefined();
    expect(pyInt("1e3")).toBeUndefined();
  });

  it("refuses a hex literal", () => {
    expect(pyInt("0x10")).toBeUndefined();
  });

  it("takes a boolean as one or zero", () => {
    expect(pyInt(true)).toBe(1);
    expect(pyInt(false)).toBe(0);
  });

  it("refuses anything that is not a number, string or boolean", () => {
    for (const value of [null, undefined, {}, [1, 2], () => 1]) {
      expect(pyInt(value)).toBeUndefined();
    }
  });

  it("refuses a value that is not finite", () => {
    expect(pyInt(Number.NaN)).toBeUndefined();
    expect(pyInt(Number.POSITIVE_INFINITY)).toBeUndefined();
  });

  it("keeps an integer beyond the safe range exact", () => {
    // A caller can send a number wider than a double; parsing it as one
    // would round it silently.
    const record = golden.safe_ints.find((entry) => entry.name === "very large");
    expect(safeInt(record?.value, 80)).toBe(record?.result);
  });

  it("handles an empty string", () => {
    expect(pyInt("")).toBeUndefined();
    expect(pyInt("   ")).toBeUndefined();
  });
});
