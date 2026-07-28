//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { computeDivergence } from "./index.ts";

interface PyDifflibGolden {
  divergences: Array<{ name: string; outputs: string[]; threshold: number; flags: boolean[] }>;
}

const golden = loadGolden<PyDifflibGolden>("pydifflib_golden.json");

describe("computeDivergence", () => {
  it.each(golden.divergences)("$name", (record) => {
    // This decides which hosts in a fan-out group are flagged as having gone
    // their own way, so a wrong flag either hides a failure or cries wolf.
    expect(computeDivergence(record.outputs, record.threshold)).toStrictEqual(record.flags);
  });

  it("has nothing to say about a single session", () => {
    // There is no consensus to diverge from, so it cannot be divergent.
    expect(computeDivergence(["anything"], 0.99)).toStrictEqual([false]);
  });

  it("flags both sides when two sessions disagree", () => {
    // With two outputs neither is the majority, so the pair is reported
    // rather than one being arbitrarily blamed.
    const record = golden.divergences.find((entry) => entry.name === "two different");
    expect(record?.flags).toStrictEqual([true, true]);
  });

  it("flags every session when none agree", () => {
    // The majority is only a majority if something else is close to it;
    // otherwise there is no consensus and the whole group is divergent.
    const record = golden.divergences.find((entry) => entry.name === "all three different");
    expect(record?.flags).toStrictEqual([true, true, true]);
  });

  it("clears the majority when it has support", () => {
    const record = golden.divergences.find((entry) => entry.name === "one odd one out");
    expect(record?.flags).toStrictEqual([false, false, true]);
  });

  it("follows the threshold it is given", () => {
    // The same three outputs are all divergent under a strict threshold and
    // all in agreement under a loose one.
    const strict = golden.divergences.find((entry) => entry.name === "near misses under a strict threshold");
    const loose = golden.divergences.find((entry) => entry.name === "near misses under a loose threshold");
    expect(strict?.outputs).toStrictEqual(loose?.outputs);
    expect(strict?.flags).toStrictEqual([true, true, true]);
    expect(loose?.flags).toStrictEqual([false, false, false]);
  });

  it("treats an empty group as having nothing to report", () => {
    expect(computeDivergence([], 0.8)).toStrictEqual([]);
  });
});
