//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { AUTOJUNK_MIN_LENGTH, matchingBlocks, sequenceRatio } from "./index.ts";

interface PyDifflibGolden {
  autojunk_min_length: number;
  ratios: Array<{ name: string; a: string; b: string; ratio: number; blocks: number[][] }>;
}

const golden = loadGolden<PyDifflibGolden>("pydifflib_golden.json");

describe("sequenceRatio", () => {
  it.each(golden.ratios)("$name", (record) => {
    // Exactly CPython's number: this feeds a divergence threshold, so being
    // close is being wrong by a configurable amount.
    expect(sequenceRatio(record.a, record.b)).toBe(record.ratio);
  });

  it("calls two empty sequences identical", () => {
    // Not zero: there is nothing to disagree about.
    expect(sequenceRatio("", "")).toBe(1);
  });

  it("is not symmetric", () => {
    // Not a curiosity: the recorded pair scores zero one way and 0.60 the
    // other, because the autojunk index is built from the right-hand
    // sequence alone. A port that ordered its arguments for convenience
    // would silently change every verdict.
    const forwards = golden.ratios.find((entry) => entry.name === "asymmetric, forwards");
    const backwards = golden.ratios.find((entry) => entry.name === "asymmetric, backwards");
    expect(forwards?.ratio).toBe(0);
    expect(backwards?.ratio).toBeGreaterThan(0.5);
    expect(sequenceRatio(forwards?.a ?? "", forwards?.b ?? "")).toBe(forwards?.ratio);
    expect(sequenceRatio(backwards?.a ?? "", backwards?.b ?? "")).toBe(backwards?.ratio);
  });
});

describe("matchingBlocks", () => {
  it.each(golden.ratios)("$name", (record) => {
    // The total can be right while the blocks are wrong, and the blocks are
    // what the recursion depends on — so they are pinned as well.
    expect(matchingBlocks(record.a, record.b).map((block) => [block.a, block.b, block.size])).toStrictEqual(
      record.blocks,
    );
  });

  it("always ends with a zero-length sentinel", () => {
    const blocks = matchingBlocks("abc", "abc");
    expect(blocks.at(-1)).toStrictEqual({ a: 3, b: 3, size: 0 });
  });

  it("merges adjacent blocks", () => {
    // Recursion can produce two touching blocks; CPython collapses them, and
    // leaving them apart would change the block list a caller sees.
    expect(matchingBlocks("abcd", "abcd")).toStrictEqual([
      { a: 0, b: 0, size: 4 },
      { a: 4, b: 4, size: 0 },
    ]);
  });
});

describe("autojunk", () => {
  it("drops popular elements once the right sequence is long enough", () => {
    // The heuristic that makes a naive port wrong on real terminal output:
    // above the threshold, an element in more than 1% of positions can no
    // longer start a match, and the ratio drops sharply.
    const record = golden.ratios.find((entry) => entry.name === "long, near-identical prompts");
    expect(record?.b.length).toBeGreaterThanOrEqual(AUTOJUNK_MIN_LENGTH);
    // Near-identical text, yet the recorded similarity is tiny.
    expect(record?.ratio).toBeLessThan(0.2);
    expect(sequenceRatio(record?.a ?? "", record?.b ?? "")).toBe(record?.ratio);
  });

  it("does not engage below the threshold", () => {
    const under = golden.ratios.find((entry) => entry.name === "just under the autojunk threshold");
    expect(under?.b.length).toBeLessThan(AUTOJUNK_MIN_LENGTH);
    expect(under?.ratio).toBeGreaterThan(0.99);
  });

  it("still lets a popular element extend a match it did not start", () => {
    // The index only decides where a match may begin. A run of spaces still
    // extends one, which is why the space-dominated case scores high.
    const record = golden.ratios.find((entry) => entry.name === "long, dominated by spaces");
    expect(record?.ratio).toBeGreaterThan(0.9);
    expect(sequenceRatio(record?.a ?? "", record?.b ?? "")).toBe(record?.ratio);
  });

  it("keeps an element sitting exactly on the popularity limit", () => {
    // The cutoff is strictly greater-than, against `len(b) / 100 + 1`. At a
    // b of exactly 200 that limit is 3: three occurrences survive and can
    // still seed a match, four cannot. A `>=` comparison, or a limit without
    // the `+1`, turns the first of these from a real match into nothing.
    const kept = golden.ratios.find((entry) => entry.name === "element exactly at the autojunk limit");
    const dropped = golden.ratios.find((entry) => entry.name === "element one past the autojunk limit");
    expect(kept?.ratio).toBeGreaterThan(0);
    expect(dropped?.ratio).toBe(0);
    expect(sequenceRatio(kept?.a ?? "", kept?.b ?? "")).toBe(kept?.ratio);
    expect(sequenceRatio(dropped?.a ?? "", dropped?.b ?? "")).toBe(dropped?.ratio);
  });

  it("extends a match backwards over dropped elements", () => {
    // The other half of the rule: a popular element cannot seed a match, but
    // it can lengthen one. Here 200 spaces are dropped from the index, the
    // match seeds on the unique tail, and the block still begins at offset
    // zero — so the two sequences score as identical.
    const record = golden.ratios.find((entry) => entry.name === "match extends back over dropped elements");
    expect(record?.ratio).toBe(1);
    expect(matchingBlocks(record?.a ?? "", record?.b ?? "")[0]).toStrictEqual({
      a: 0,
      b: 0,
      size: record?.a.length,
    });
  });

  it("extends backwards when nothing else can reach the shared prefix", () => {
    // The case that isolates the backwards walk. The seed lands at b=0, so
    // the recursion never explores to its left — without the walk the 200
    // shared spaces before it are simply lost and the ratio collapses.
    const record = golden.ratios.find((entry) => entry.name === "backwards extension is the only way left");
    expect(record?.ratio).toBeGreaterThan(0.99);
    expect(matchingBlocks(record?.a ?? "", record?.b ?? "")[0]).toStrictEqual({ a: 2, b: 0, size: 204 });
  });

  it("reports one block, not two, across a dropped run", () => {
    // The backwards walk absorbs the shared prefix into the seeded block, so
    // the result is a single span rather than two abutting ones. (That is
    // also why the merge pass never fires without a junk predicate.)
    const record = golden.ratios.find((entry) => entry.name === "match extends back over dropped elements");
    expect(matchingBlocks(record?.a ?? "", record?.b ?? "")).toStrictEqual([
      { a: 0, b: 0, size: 211 },
      { a: 211, b: 211, size: 0 },
    ]);
  });

  it("exposes the reference threshold", () => {
    expect(AUTOJUNK_MIN_LENGTH).toBe(golden.autojunk_min_length);
  });
});
