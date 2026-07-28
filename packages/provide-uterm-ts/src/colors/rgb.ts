//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * RGB-to-palette-index mapping helpers.
 *
 * Given an (R, G, B) triple in the 0-255 range, return the nearest palette
 * index for either the xterm-256 cube ({@link rgbTo256}) or the base
 * 16-color ANSI palette ({@link rgbTo16Index}).
 *
 * Port of the Python module `provide.uterm.colors.rgb`.
 */

import { pyRound } from "../pycompat/index.ts";

/**
 * BBS-canonical 16-color reference palette. Values match typical xterm /
 * PuTTY defaults (index 0 = black, 15 = bright white). Euclidean distance in
 * RGB space is used to find the nearest index.
 */
const PALETTE_16: ReadonlyArray<readonly [number, number, number]> = [
  [0, 0, 0],
  [0, 0, 205],
  [0, 205, 0],
  [0, 205, 205],
  [205, 0, 0],
  [205, 0, 205],
  [205, 205, 0],
  [229, 229, 229],
  [127, 127, 127],
  [92, 92, 255],
  [92, 255, 92],
  [92, 255, 255],
  [255, 92, 92],
  [255, 92, 255],
  [255, 255, 92],
  [255, 255, 255],
];

/** Clamp an integer to the 0-255 range. */
function clamp8(v: number): number {
  if (v < 0) {
    return 0;
  }
  if (v > 255) {
    return 255;
  }
  return v;
}

/**
 * Map an (R, G, B) triple to the nearest xterm-256 palette index.
 *
 * Uses the standard 6x6x6 color cube (indices 16-231) plus the 24-step
 * greyscale ramp (indices 232-255). When R == G == B the greyscale ramp is
 * preferred for finer luminance resolution.
 *
 * @param r Red component, 0-255 (clamped).
 * @param g Green component, 0-255 (clamped).
 * @param b Blue component, 0-255 (clamped).
 * @returns xterm-256 palette index (16-255).
 */
export function rgbTo256(r: number, g: number, b: number): number {
  const rr = clamp8(r);
  const gg = clamp8(g);
  const bb = clamp8(b);
  if (rr === gg && gg === bb) {
    if (rr < 8) {
      return 16;
    }
    if (rr > 248) {
      return 231;
    }
    return 232 + Math.trunc(((rr - 8) / 247) * 24);
  }
  const rc = pyRound((rr / 255) * 5);
  const gc = pyRound((gg / 255) * 5);
  const bc = pyRound((bb / 255) * 5);
  return 16 + 36 * rc + 6 * gc + bc;
}

/**
 * Map an (R, G, B) triple to the nearest base-16 ANSI palette index.
 *
 * Uses Euclidean distance in RGB space against the reference palette
 * (xterm/PuTTY defaults). Returns an index 0-15 where 0-7 are the normal
 * colors and 8-15 are the bright variants.
 *
 * @param r Red component, 0-255 (clamped).
 * @param g Green component, 0-255 (clamped).
 * @param b Blue component, 0-255 (clamped).
 * @returns Palette index 0-15.
 */
export function rgbTo16Index(r: number, g: number, b: number): number {
  const rr = clamp8(r);
  const gg = clamp8(g);
  const bb = clamp8(b);
  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let i = 0; i < PALETTE_16.length; i += 1) {
    const [tr, tg, tb] = PALETTE_16[i] as readonly [number, number, number];
    const d = (rr - tr) * (rr - tr) + (gg - tg) * (gg - tg) + (bb - tb) * (bb - tb);
    if (d < bestDistance) {
      bestDistance = d;
      bestIndex = i;
    }
  }
  return bestIndex;
}
