//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { pyRound } from "./index.ts";

describe("pyRound", () => {
  // Every expectation below is the literal output of CPython's `round()`.
  it.each([
    ["leaves an integer alone", 0, 0],
    ["rounds below the midpoint down", 0.4, 0],
    ["breaks a tie towards even (0.5 → 0)", 0.5, 0],
    ["rounds above the midpoint up", 0.6, 1],
    ["breaks a tie towards even (1.5 → 2)", 1.5, 2],
    ["breaks a tie towards even (2.5 → 2)", 2.5, 2],
    ["breaks a tie towards even (3.5 → 4)", 3.5, 4],
    ["breaks a tie towards even (4.5 → 4)", 4.5, 4],
    ["rounds just below a tie down", 2.4999, 2],
    ["rounds just above a tie up", 2.5001, 3],
    ["rounds the largest double below a tie down", 0.49999999999999994, 0],
    ["breaks a tie towards even at large magnitude", 1000000000000000.5, 1000000000000000],
  ])("%s", (_name, input, want) => {
    expect(pyRound(input)).toBe(want);
  });

  it.each([
    ["breaks a negative tie towards even (-0.5 → 0)", -0.5, 0],
    ["breaks a negative tie towards even (-1.5 → -2)", -1.5, -2],
    ["breaks a negative tie towards even (-2.5 → -2)", -2.5, -2],
    ["rounds a negative below the midpoint towards zero", -0.4, 0],
    ["rounds a negative above the midpoint away from zero", -0.6, -1],
  ])("%s", (_name, input, want) => {
    expect(pyRound(input)).toBe(want);
  });

  it("never returns negative zero, matching CPython's integer result", () => {
    expect(Object.is(pyRound(-0.5), 0)).toBe(true);
    expect(Object.is(pyRound(-0.4), 0)).toBe(true);
  });
});
