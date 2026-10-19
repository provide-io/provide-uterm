//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/** How many cells a character takes, and whether it combines, as the reference measures them. */

import { COMBINING_CLASSES, WIDTH_RANGES } from "./wcwidth-tables.ts";

/**
 * Find the run in `runs` (flat groups of `stride` numbers, each starting
 * `first, last`, sorted and disjoint) that holds `point`. Returns the index of
 * the run's first number, or -1 when no run holds it.
 */
function findRun(runs: readonly number[], stride: number, point: number): number {
  let low = 0;
  let high = runs.length / stride - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const at = middle * stride;
    if (point < (runs[at] as number)) {
      high = middle - 1;
    } else if (point > (runs[at + 1] as number)) {
      low = middle + 1;
    } else {
      return at;
    }
  }
  return -1;
}

/**
 * The cells the first code point of `data` takes: 2 for a wide one, 1 for
 * most, 0 for one that takes none (a combining mark, a zero width space), and
 * -1 for a control. This is wcwidth's answer, which is what the reference
 * draws with. `data` must not be empty.
 */
export function charWidth(data: string): number {
  const at = findRun(WIDTH_RANGES, 3, data.codePointAt(0) as number);
  return at < 0 ? 1 : (WIDTH_RANGES[at + 2] as number);
}

/** The canonical combining class of `point`: 0 for a starter. */
export function combiningClass(point: number): number {
  const at = findRun(COMBINING_CLASSES, 3, point);
  return at < 0 ? 0 : (COMBINING_CLASSES[at + 2] as number);
}

/** Whether the first code point of `data` has a non-zero combining class. */
export function isCombining(data: string): boolean {
  return combiningClass(data.codePointAt(0) as number) > 0;
}
