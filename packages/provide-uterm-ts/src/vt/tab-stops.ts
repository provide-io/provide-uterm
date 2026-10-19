//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/** Distance between the tab stops a screen powers on with. */
const TAB_WIDTH = 8;

/**
 * A screen's tab stops: the columns HTS has set and TBC has not cleared, as
 * the reference keeps them. Held ascending, so a tab finds the next one by
 * binary search, with nothing to sort or build per tab.
 */
export class TabStops {
  #stops: number[] = [];

  /** The power-on stops: every eighth column below `cols`, the first at column 8. */
  constructor(cols: number) {
    for (let x = TAB_WIDTH; x < cols; x += TAB_WIDTH) {
      this.#stops.push(x);
    }
  }

  /** The index of the first stop above `x` (the stop count if none). */
  #firstAbove(x: number): number {
    let low = 0;
    let high = this.#stops.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if ((this.#stops[middle] as number) > x) {
        high = middle;
      } else {
        low = middle + 1;
      }
    }
    return low;
  }

  /** The first stop above `x`, or `undefined` when there is none. */
  after(x: number): number | undefined {
    return this.#stops[this.#firstAbove(x)];
  }

  /** Set a stop at `x`. */
  set(x: number): void {
    const at = this.#firstAbove(x);
    if (this.#stops[at - 1] !== x) {
      this.#stops.splice(at, 0, x);
    }
  }

  /** Clear the stop at `x`, if there is one. */
  clear(x: number): void {
    const at = this.#firstAbove(x) - 1;
    if (this.#stops[at] === x) {
      this.#stops.splice(at, 1);
    }
  }

  /** Clear every stop. */
  clearAll(): void {
    this.#stops = [];
  }
}
