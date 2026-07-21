//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";

import {
  closeLeaf,
  collectLeaves,
  initialLayout,
  leaf,
  leafCount,
  makeIdGen,
  type Node,
  nautilusAdd,
  type Split,
  splitLeaf,
  unit,
} from "./panels-model.js";

describe("panels-model", () => {
  it("initial layout is one VNC+terminal split", () => {
    const t = initialLayout(makeIdGen());
    expect(t.kind).toBe("split");
    const leaves = collectLeaves(t);
    expect(leaves.map((l) => l.pane)).toEqual(["vnc", "terminal"]);
    expect(leafCount(t)).toBe(2);
  });

  it("unit places VNC first, terminal second", () => {
    const u = unit(makeIdGen());
    expect(u.first).toMatchObject({ kind: "leaf", pane: "vnc" });
    expect(u.second).toMatchObject({ kind: "leaf", pane: "terminal" });
  });

  it("splitLeaf turns a leaf into a split with the opposite pane, keeping the id", () => {
    const l = leaf("keep", "vnc");
    const out = splitLeaf(l, "keep", makeIdGen()) as Split;
    expect(out.kind).toBe("split");
    expect(out.first).toEqual(leaf("keep", "vnc"));
    expect((out.second as { pane: string }).pane).toBe("terminal");
  });

  it("splitLeaf on a terminal produces a terminal|vnc split", () => {
    const out = splitLeaf(leaf("t", "terminal"), "t", makeIdGen()) as Split;
    expect((out.first as { pane: string }).pane).toBe("terminal");
    expect((out.second as { pane: string }).pane).toBe("vnc");
  });

  it("splitLeaf is a no-op for an unknown id", () => {
    const l = leaf("a", "vnc");
    expect(splitLeaf(l, "missing", makeIdGen())).toBe(l);
  });

  it("splitLeaf alternates orientation with depth", () => {
    const mk = makeIdGen();
    const t = initialLayout(mk) as Split; // row
    const targetLeaf = collectLeaves(t)[0].id;
    const out = splitLeaf(t, targetLeaf, mk) as Split;
    // nested split under a row parent should be a column
    const nested = out.first as Split;
    expect(out.dir).toBe("row");
    expect(nested.dir).toBe("col");
  });

  it("splitLeaf grows the leaf count by one", () => {
    const mk = makeIdGen();
    let t: Node = initialLayout(mk);
    const id = collectLeaves(t)[0].id;
    t = splitLeaf(t, id, mk);
    expect(leafCount(t)).toBe(3);
  });

  it("closeLeaf collapses the sibling into the parent slot", () => {
    const mk = makeIdGen();
    const t = initialLayout(mk) as Split;
    const vncId = collectLeaves(t)[0].id;
    const out = closeLeaf(t, vncId);
    expect(out).toMatchObject({ kind: "leaf", pane: "terminal" });
  });

  it("closeLeaf returns null when the last pane is removed", () => {
    const l = leaf("only", "vnc");
    expect(closeLeaf(l, "only")).toBeNull();
  });

  it("closeLeaf is a no-op for an unknown id", () => {
    const mk = makeIdGen();
    const t = initialLayout(mk);
    expect(closeLeaf(t, "nope")).toBe(t);
  });

  it("nautilusAdd wraps the layout and adds a fresh unit each step", () => {
    const mk = makeIdGen();
    let t: Node = initialLayout(mk);
    const before = leafCount(t);
    t = nautilusAdd(t, mk, 0);
    expect(leafCount(t)).toBe(before + 2); // a fresh VNC+terminal unit
  });

  it("nautilusAdd rotates direction and side each step (spiral)", () => {
    const mk = makeIdGen();
    const base = initialLayout(mk);
    const s0 = nautilusAdd(base, mk, 0);
    const s1 = nautilusAdd(base, mk, 1);
    const s2 = nautilusAdd(base, mk, 2);
    expect(s0.dir).toBe("row");
    expect(s1.dir).toBe("col");
    // step 0/1 keep existing layout leading; step 2/3 flip it trailing
    expect(s0.first).toBe(base);
    expect(s2.second).toBe(base);
    expect(s0.ratio).toBeGreaterThan(0.5);
    expect(s2.ratio).toBeLessThan(0.5);
  });

  it("makeIdGen yields unique monotonic ids", () => {
    const mk = makeIdGen("x");
    const ids = [mk(), mk(), mk()];
    expect(ids).toEqual(["x0", "x1", "x2"]);
    expect(new Set(ids).size).toBe(3);
  });
});
