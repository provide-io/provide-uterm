//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * NFC, from the reference's Unicode data rather than the runtime's.
 *
 * A combining mark drawn onto a cell is composed with it (the reference calls
 * `unicodedata.normalize("NFC", ...)`, pinned to unicodedata2). The runtime's
 * `String.prototype.normalize` follows whatever Unicode version its ICU ships,
 * which is not the reference's and moves with every Node upgrade: under a
 * newer one a pair the reference leaves apart composes. This works from the
 * generated tables instead (./normalization-tables.ts, ./wcwidth-tables.ts),
 * as the Go port's normalize.go does, with Hangul composed algorithmically.
 */

import { CANONICAL_DECOMPOSITIONS, COMPOSITION_PAIRS } from "./normalization-tables.ts";
import { combiningClass } from "./width.ts";

/** Hangul syllable composition constants (Unicode chapter 3.12). */
const S_BASE = 0xac00;
const L_BASE = 0x1100;
const V_BASE = 0x1161;
const T_BASE = 0x11a7;
const L_COUNT = 19;
const V_COUNT = 21;
const T_COUNT = 28;
const N_COUNT = V_COUNT * T_COUNT;
const S_COUNT = L_COUNT * N_COUNT;

/** Above every code point, so `first * PAIR + second` keys a pair uniquely. */
const PAIR = 0x110000;

const DECOMPOSITIONS = new Map<number, readonly [number, number]>();
for (let at = 0; at < CANONICAL_DECOMPOSITIONS.length; at += 3) {
  DECOMPOSITIONS.set(CANONICAL_DECOMPOSITIONS[at] as number, [
    CANONICAL_DECOMPOSITIONS[at + 1] as number,
    CANONICAL_DECOMPOSITIONS[at + 2] as number,
  ]);
}

const COMPOSITIONS = new Map<number, number>();
for (let at = 0; at < COMPOSITION_PAIRS.length; at += 3) {
  COMPOSITIONS.set(
    (COMPOSITION_PAIRS[at] as number) * PAIR + (COMPOSITION_PAIRS[at + 1] as number),
    COMPOSITION_PAIRS[at + 2] as number,
  );
}

/** Append the full canonical decomposition of `point` to `out`. */
function decompose(out: number[], point: number): void {
  if (point >= S_BASE && point < S_BASE + S_COUNT) {
    const index = point - S_BASE;
    out.push(L_BASE + Math.floor(index / N_COUNT), V_BASE + Math.floor((index % N_COUNT) / T_COUNT));
    if (index % T_COUNT !== 0) {
      out.push(T_BASE + (index % T_COUNT));
    }
    return;
  }
  const mapping = DECOMPOSITIONS.get(point);
  if (mapping === undefined) {
    out.push(point);
    return;
  }
  decompose(out, mapping[0]);
  if (mapping[1] >= 0) {
    decompose(out, mapping[1]);
  }
}

/** Sort each run of non-starters by combining class, stably (canonical ordering). */
function canonicalOrder(points: number[]): void {
  for (let i = 1; i < points.length; i += 1) {
    const cc = combiningClass(points[i] as number);
    if (cc === 0) {
      continue;
    }
    for (let j = i; j > 0; j -= 1) {
      const before = combiningClass(points[j - 1] as number);
      if (before === 0 || before <= cc) {
        break;
      }
      [points[j - 1], points[j]] = [points[j] as number, points[j - 1] as number];
    }
  }
}

/** The primary composite of `first` and `second`, or `undefined`. */
function composePair(first: number, second: number): number | undefined {
  if (first >= L_BASE && first < L_BASE + L_COUNT && second >= V_BASE && second < V_BASE + V_COUNT) {
    return S_BASE + ((first - L_BASE) * V_COUNT + (second - V_BASE)) * T_COUNT;
  }
  if (
    first >= S_BASE &&
    first < S_BASE + S_COUNT &&
    (first - S_BASE) % T_COUNT === 0 &&
    second > T_BASE &&
    second < T_BASE + T_COUNT
  ) {
    return first + (second - T_BASE);
  }
  return COMPOSITIONS.get(first * PAIR + second);
}

/**
 * Canonical composition over a decomposed, canonically ordered sequence. A
 * mark composes with the last starter unless something between them blocks
 * it: a non-starter of the same or a higher class. (When the last character
 * is a starter it is the last starter, so nothing lies between them.)
 */
function compose(points: number[]): number[] {
  const out: number[] = [];
  let starter = -1;
  let lastClass = 0;
  for (const point of points) {
    const cc = combiningClass(point);
    if (starter >= 0 && (lastClass === 0 || lastClass < cc)) {
      const composite = composePair(out[starter] as number, point);
      if (composite !== undefined) {
        out[starter] = composite;
        continue;
      }
    }
    out.push(point);
    if (cc === 0) {
      starter = out.length - 1;
    }
    lastClass = cc;
  }
  return out;
}

/** `text` in NFC, as the reference's `unicodedata.normalize("NFC", text)` gives it. */
export function nfc(text: string): string {
  const points: number[] = [];
  for (const char of text) {
    decompose(points, char.codePointAt(0) as number);
  }
  canonicalOrder(points);
  return String.fromCodePoint(...compose(points));
}
