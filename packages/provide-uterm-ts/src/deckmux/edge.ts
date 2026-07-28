//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Edge indicator geometry.
 *
 * Port of the Python module `provide.uterm.deckmux._edge`.
 *
 * These are fractions a browser turns into pixels, quantised to four places
 * so they travel as short decimals. The quantiser is CPython's, ties and all
 * — a bar drawn from a differently-rounded fraction sits on a different row
 * from everybody else's.
 */

import { pyRoundTo } from "../pycompat/index.ts";

/** How many places an edge fraction carries. */
const EDGE_PLACES = 4;

/**
 * Where a viewport's bar sits, as `[top, height]` fractions.
 *
 * An empty buffer fills the bar rather than dividing by zero: there is
 * nothing to scroll, so all of it is on screen.
 */
export function viewportToEdgeRange(scrollTopLine: number, visibleLines: number, totalLines: number): [number, number] {
  if (totalLines <= 0) {
    return [0.0, 1.0];
  }
  const topPct = scrollTopLine / totalLines;
  // Clamped by what is left below the top, so a viewport taller than the rest
  // of the buffer does not run the bar off the end of its track.
  const heightPct = Math.min(visibleLines / totalLines, 1.0 - topPct);
  return [pyRoundTo(topPct, EDGE_PLACES), pyRoundTo(heightPct, EDGE_PLACES)];
}

/**
 * Where a single line sits on the bar, as a fraction.
 *
 * Clamped to the end: a stale line number from a browser must not point off
 * the track.
 */
export function lineToEdgePosition(line: number, totalLines: number): number {
  if (totalLines <= 0) {
    return 0.0;
  }
  return pyRoundTo(Math.min(line / totalLines, 1.0), EDGE_PLACES);
}

/** The line at the centre of a viewport. */
export function scrollCenterLine(scrollTop: number, visibleLines: number): number {
  // Floored: half a line is not somewhere to scroll to.
  return scrollTop + Math.floor(visibleLines / 2);
}
