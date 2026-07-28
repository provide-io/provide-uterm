//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * BBS colour palette constants and 256-colour decoding.
 *
 * Port of the palette tables in the Python module `provide.uterm.ansi`.
 */

/** 256-colour palette indices that map the 16 base BBS colours. */
export const DEFAULT_PALETTE: number[] = [
  0, // black
  160, // red
  34, // green
  184, // yellow/brown
  27, // blue
  127, // magenta
  37, // cyan
  252, // white
  244, // bright black / gray
  196, // bright red
  46, // bright green
  226, // bright yellow
  39, // bright blue (was 51 = (0,255,255) cyan — a mislabel; 39 = (0,175,255))
  201, // bright magenta
  87, // bright cyan
  231, // bright white
];

/**
 * Direct RGB tuples for the 16 base BBS colours (truecolor output).
 *
 * Base red/blue/magenta were lifted for WCAG AA (4.5:1) on the #0a0a0a
 * terminal background; the bright variants at indices 9, 12 and 13 are
 * unchanged.
 */
export const DEFAULT_RGB: Array<readonly [number, number, number]> = [
  [0, 0, 0], // black
  [235, 77, 77], // red
  [0, 175, 0], // green
  [215, 175, 0], // yellow/brown
  [64, 128, 255], // blue
  [224, 48, 224], // magenta
  [0, 175, 175], // cyan
  [208, 208, 208], // white
  [128, 128, 128], // bright black / gray
  [255, 0, 0], // bright red
  [0, 255, 0], // bright green
  [255, 255, 0], // bright yellow
  [0, 175, 255], // bright blue
  [255, 0, 255], // bright magenta
  [95, 255, 255], // bright cyan
  [255, 255, 255], // bright white
];

/** Clear the screen and home the cursor. */
export const CLEAR_SCREEN = "\x1b[2J\x1b[H";
/** Enable bold. */
export const BOLD = "\x1b[1m";
/** Reset all attributes. */
export const RESET = "\x1b[0m";

/** Component levels of the 6x6x6 colour cube. */
const CUBE_LEVELS = [0, 95, 135, 175, 215, 255] as const;

/** Convert a 256-colour index to an (R, G, B) triple. */
export function color256ToRgb(index: number): [number, number, number] {
  if (index < 16) {
    return [...(DEFAULT_RGB[index] as readonly [number, number, number])];
  }
  if (index < 232) {
    let remainder = index - 16;
    const b = remainder % 6;
    remainder = Math.floor(remainder / 6);
    const g = remainder % 6;
    const r = Math.floor(remainder / 6);
    return [CUBE_LEVELS[r] as number, CUBE_LEVELS[g] as number, CUBE_LEVELS[b] as number];
  }
  const gray = 8 + (index - 232) * 10;
  return [gray, gray, gray];
}
