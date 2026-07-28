//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython's `difflib.SequenceMatcher` similarity, on the ECMAScript engine.
 *
 * The measure is Ratcliff/Obershelp, not edit distance and not longest common
 * subsequence: take the longest matching block, recurse on what is left to
 * either side of it, and report twice the total matched length over the
 * combined length. Two sequences with the same edit distance can score very
 * differently under it.
 *
 * It is reproduced here rather than approximated because the number feeds a
 * divergence threshold — being close is being wrong by a configurable amount.
 */

/** Length at which the autojunk heuristic starts applying to `b`. */
export const AUTOJUNK_MIN_LENGTH = 200;

/** A run of elements common to both sequences. */
export interface MatchingBlock {
  /** Offset into the first sequence. */
  a: number;
  /** Offset into the second sequence. */
  b: number;
  /** How many elements match. */
  size: number;
}

/**
 * Index of `b`, from element to the positions it occupies.
 *
 * Above {@link AUTOJUNK_MIN_LENGTH}, any element occupying more than 1% of
 * the positions is dropped from the index entirely. That is the autojunk
 * heuristic, and on terminal output — dense with spaces and newlines — it
 * fires constantly and lowers the ratio substantially. A port without it
 * reports two screens as far more similar than CPython does, and a threshold
 * tuned against CPython stops firing.
 *
 * The index only decides where a match may *begin*: a dropped element can
 * still extend a match that started elsewhere.
 */
function indexOfB(b: string): Map<string, number[]> {
  const index = new Map<string, number[]>();
  for (let position = 0; position < b.length; position += 1) {
    const element = b[position] as string;
    const positions = index.get(element);
    if (positions === undefined) {
      index.set(element, [position]);
    } else {
      positions.push(position);
    }
  }
  if (b.length >= AUTOJUNK_MIN_LENGTH) {
    const limit = Math.floor(b.length / 100) + 1;
    for (const [element, positions] of [...index]) {
      if (positions.length > limit) {
        index.delete(element);
      }
    }
  }
  return index;
}

/**
 * The longest block common to `a[aLo:aHi]` and `b[bLo:bHi]`.
 *
 * Ties go to the earliest block in `a`, then the earliest in `b`. That is not
 * arbitrary: a different tie-break picks a different block, which changes
 * what is left to recurse on and therefore the final total.
 */
function findLongestMatch(
  a: string,
  b: string,
  index: Map<string, number[]>,
  aLo: number,
  aHi: number,
  bLo: number,
  bHi: number,
): MatchingBlock {
  let bestI = aLo;
  let bestJ = bLo;
  let bestSize = 0;
  // Length of the run ending at each position of b, for the previous i.
  let runLengths = new Map<number, number>();

  for (let i = aLo; i < aHi; i += 1) {
    const nextRunLengths = new Map<number, number>();
    for (const j of index.get(a[i] as string) ?? []) {
      if (j < bLo) {
        continue;
      }
      if (j >= bHi) {
        break;
      }
      const length = (runLengths.get(j - 1) ?? 0) + 1;
      nextRunLengths.set(j, length);
      if (length > bestSize) {
        bestI = i - length + 1;
        bestJ = j - length + 1;
        bestSize = length;
      }
    }
    runLengths = nextRunLengths;
  }

  // Extend through elements the index dropped: they cannot start a match but
  // they can lengthen one.
  while (bestI > aLo && bestJ > bLo && a[bestI - 1] === b[bestJ - 1]) {
    bestI -= 1;
    bestJ -= 1;
    bestSize += 1;
  }
  while (bestI + bestSize < aHi && bestJ + bestSize < bHi && a[bestI + bestSize] === b[bestJ + bestSize]) {
    bestSize += 1;
  }
  return { a: bestI, b: bestJ, size: bestSize };
}

/**
 * The matching blocks between `a` and `b`, in order.
 *
 * Always ends with a zero-length sentinel at the end of both sequences, which
 * callers rely on to terminate a walk.
 *
 * The merge pass that follows is unreachable with no junk predicate, and is
 * kept for fidelity with CPython, where it exists for the case one is
 * supplied. The reason it cannot fire: a left-recursion block ending exactly
 * where the seeded block begins implies the two are contiguous equal runs,
 * and the backwards extension above would already have absorbed it. Confirmed
 * exhaustively over all 65,025 pairs of binary strings up to length seven.
 *
 * Worth stating plainly because it is easy to convince yourself otherwise:
 * disable the backwards extension and adjacency does appear, so probing a
 * broken build makes this pass look load-bearing when it is not.
 */
export function matchingBlocks(a: string, b: string): MatchingBlock[] {
  const index = indexOfB(b);
  const queue: Array<[number, number, number, number]> = [[0, a.length, 0, b.length]];
  const found: MatchingBlock[] = [];

  while (queue.length > 0) {
    const [aLo, aHi, bLo, bHi] = queue.pop() as [number, number, number, number];
    const block = findLongestMatch(a, b, index, aLo, aHi, bLo, bHi);
    if (block.size === 0) {
      continue;
    }
    found.push(block);
    if (aLo < block.a && bLo < block.b) {
      queue.push([aLo, block.a, bLo, block.b]);
    }
    if (block.a + block.size < aHi && block.b + block.size < bHi) {
      queue.push([block.a + block.size, aHi, block.b + block.size, bHi]);
    }
  }
  // By `a` alone: the recursion only ever explores disjoint ranges, so no two
  // blocks share a starting offset. CPython sorts the triples, which compares
  // `b` and then the size as tie-breaks that can never be reached.
  found.sort((left, right) => left.a - right.a);

  const merged: MatchingBlock[] = [];
  let a1 = 0;
  let b1 = 0;
  let size1 = 0;
  for (const block of found) {
    if (a1 + size1 === block.a && b1 + size1 === block.b) {
      size1 += block.size;
      continue;
    }
    if (size1 > 0) {
      merged.push({ a: a1, b: b1, size: size1 });
    }
    a1 = block.a;
    b1 = block.b;
    size1 = block.size;
  }
  if (size1 > 0) {
    merged.push({ a: a1, b: b1, size: size1 });
  }
  merged.push({ a: a.length, b: b.length, size: 0 });
  return merged;
}

/**
 * CPython's `difflib.SequenceMatcher(None, a, b).ratio()`.
 *
 * Twice the matched length over the combined length. Two empty sequences
 * score 1: there is nothing to disagree about.
 *
 * Not symmetric — the autojunk index is built from `b` alone — so a caller
 * that reorders its arguments for convenience changes the answer.
 */
export function sequenceRatio(a: string, b: string): number {
  const total = a.length + b.length;
  if (total === 0) {
    return 1;
  }
  const matched = matchingBlocks(a, b).reduce((sum, block) => sum + block.size, 0);
  return (2 * matched) / total;
}
