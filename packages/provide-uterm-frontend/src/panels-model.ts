//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * Pure model for the recursive VNC + terminal tiling panel.
 *
 * A layout is a binary tree: a {@link Leaf} is a single live pane (VNC or
 * terminal); a {@link Split} divides its area into two children along a row or
 * column at a ratio. The "unit" of the demo is a VNC+terminal split; splitting
 * keeps subdividing, and {@link nautilusAdd} wraps the whole layout in a
 * golden-ratio spiral (the fractal / nautilus aesthetic).
 *
 * Everything here is side-effect-free and DOM-free so it unit-tests directly;
 * `panels.ts` owns rendering and the live RFB/terminal connections.
 */

export type PaneType = "vnc" | "terminal";
export type SplitDir = "row" | "col";

export interface Leaf {
  readonly kind: "leaf";
  readonly id: string;
  readonly pane: PaneType;
}

export interface Split {
  readonly kind: "split";
  readonly id: string;
  readonly dir: SplitDir;
  readonly ratio: number;
  readonly first: Node;
  readonly second: Node;
}

export type Node = Leaf | Split;

/** Monotonic id source; injectable so tests get deterministic ids. */
export type IdGen = () => string;

export function makeIdGen(prefix = "n"): IdGen {
  let n = 0;
  return () => `${prefix}${n++}`;
}

const GOLDEN = 0.618;

export function leaf(id: string, pane: PaneType): Leaf {
  return { kind: "leaf", id, pane };
}

/** A VNC+terminal "instance": VNC on the leading side, terminal trailing. */
export function unit(mkId: IdGen, dir: SplitDir = "row"): Split {
  return {
    kind: "split",
    id: mkId(),
    dir,
    ratio: 0.5,
    first: leaf(mkId(), "vnc"),
    second: leaf(mkId(), "terminal"),
  };
}

/** Initial layout: a single VNC+terminal split screen. */
export function initialLayout(mkId: IdGen): Node {
  return unit(mkId, "row");
}

export function collectLeaves(node: Node): Leaf[] {
  if (node.kind === "leaf") return [node];
  return [...collectLeaves(node.first), ...collectLeaves(node.second)];
}

export function leafCount(node: Node): number {
  return collectLeaves(node).length;
}

/**
 * Replace the leaf *leafId* with a split of [original pane, new opposite pane].
 * "Keep splitting each time" — a VNC pane splits into VNC|terminal, a terminal
 * into terminal|VNC, alternating orientation with depth for a woven look.
 */
export function splitLeaf(node: Node, leafId: string, mkId: IdGen, dir?: SplitDir): Node {
  if (node.kind === "leaf") {
    if (node.id !== leafId) return node;
    const other: PaneType = node.pane === "vnc" ? "terminal" : "vnc";
    return {
      kind: "split",
      id: mkId(),
      dir: dir ?? "row",
      ratio: 0.5,
      first: leaf(node.id, node.pane),
      second: leaf(mkId(), other),
    };
  }
  const nextDir: SplitDir = node.dir === "row" ? "col" : "row";
  const first = splitLeaf(node.first, leafId, mkId, nextDir);
  const second = first === node.first ? splitLeaf(node.second, leafId, mkId, nextDir) : node.second;
  if (first === node.first && second === node.second) return node;
  return { ...node, first, second };
}

/**
 * Remove the leaf *leafId*; its sibling collapses up into the parent's slot.
 * Returns the sibling when the whole tree collapses to one pane, or null if the
 * only leaf was removed.
 */
export function closeLeaf(node: Node, leafId: string): Node | null {
  if (node.kind === "leaf") {
    return node.id === leafId ? null : node;
  }
  const first = closeLeaf(node.first, leafId);
  if (first === null) return node.second;
  const second = closeLeaf(node.second, leafId);
  if (second === null) return node.first;
  if (first === node.first && second === node.second) return node;
  return { ...node, first, second };
}

/**
 * Wrap the whole layout in a new VNC+terminal unit, spiraling golden-ratio
 * inward. Direction and which side the existing layout keeps rotate every step
 * (right → bottom → left → top …), producing the nautilus tiling.
 */
export function nautilusAdd(node: Node, mkId: IdGen, step: number): Split {
  // Inward nautilus: each fresh VNC+terminal unit takes the golden-MAJOR cell
  // while the whole existing layout recurses into the golden-minor, and the
  // fresh side rotates through right → bottom → left → top. So the newest unit
  // is always the biggest and older panes spiral progressively smaller toward a
  // corner — a visibly self-similar (fractal) golden spiral.
  const dir: SplitDir = step % 2 === 0 ? "row" : "col";
  const freshLeading = step % 4 >= 2;
  const fresh = unit(mkId, dir === "row" ? "col" : "row");
  return {
    kind: "split",
    id: mkId(),
    dir,
    ratio: freshLeading ? GOLDEN : 1 - GOLDEN,
    first: freshLeading ? fresh : node,
    second: freshLeading ? node : fresh,
  };
}
