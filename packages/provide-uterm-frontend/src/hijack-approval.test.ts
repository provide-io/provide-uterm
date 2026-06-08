//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import { computeRemainingSeconds } from "./hijack-approval.js";

describe("computeRemainingSeconds", () => {
  it("rounds to the nearest second", () => {
    expect(computeRemainingSeconds(100, 99_400)).toBe(1);
    expect(computeRemainingSeconds(100, 90_000)).toBe(10);
    expect(computeRemainingSeconds(100, 99_600)).toBe(0);
  });

  it("clamps at zero", () => {
    expect(computeRemainingSeconds(5, 999_999_999)).toBe(0);
  });

  it("defaults nowMs to Date.now()", () => {
    const future = Date.now() / 1000 + 1000;
    const r = computeRemainingSeconds(future);
    expect(r).toBeGreaterThan(0);
  });
});
