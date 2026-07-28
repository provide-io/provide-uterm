//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * ANSI colour palettes, and mapping an arbitrary colour onto one.
 *
 * Port of `provide.uterm.render.palette`.
 *
 * Distance is squared Euclidean in RGB. Not perceptual — a perceptual metric
 * would be better and would also disagree with every other implementation of
 * this palette, which is the one thing a quantiser cannot afford.
 */

/** One standard ANSI colour: its RGB, and the codes that select it. */
export interface Ansi16Entry {
  r: number;
  g: number;
  b: number;
  /** The SGR code that sets it as a foreground. */
  fg: number;
  /** The SGR code that sets it as a background. */
  bg: number;
}

/** The sixteen standard ANSI colours, in their canonical order. */
export const ANSI16_PALETTE: readonly Ansi16Entry[] = [
  { r: 0, g: 0, b: 0, fg: 30, bg: 40 },
  { r: 170, g: 0, b: 0, fg: 31, bg: 41 },
  { r: 0, g: 170, b: 0, fg: 32, bg: 42 },
  { r: 170, g: 85, b: 0, fg: 33, bg: 43 },
  { r: 0, g: 0, b: 170, fg: 34, bg: 44 },
  { r: 170, g: 0, b: 170, fg: 35, bg: 45 },
  { r: 0, g: 170, b: 170, fg: 36, bg: 46 },
  { r: 170, g: 170, b: 170, fg: 37, bg: 47 },
  { r: 85, g: 85, b: 85, fg: 90, bg: 100 },
  { r: 255, g: 85, b: 85, fg: 91, bg: 101 },
  { r: 85, g: 255, b: 85, fg: 92, bg: 102 },
  { r: 255, g: 255, b: 85, fg: 93, bg: 103 },
  { r: 85, g: 85, b: 255, fg: 94, bg: 104 },
  { r: 255, g: 85, b: 255, fg: 95, bg: 105 },
  { r: 85, g: 255, b: 255, fg: 96, bg: 106 },
  { r: 255, g: 255, b: 255, fg: 97, bg: 107 },
];

/** An RGB triple. */
export type Rgb = readonly [number, number, number];

/** How many levels each channel of the colour cube has. */
const CUBE_LEVELS = 6;

/** The first cube step is wider than the rest, which is the easy thing to get wrong. */
const CUBE_FIRST_STEP = 55;
const CUBE_STEP = 40;

/** How many greys follow the cube, and where they start and step. */
const GREY_COUNT = 24;
const GREY_START = 8;
const GREY_STEP = 10;

/** One channel's value at a cube level. */
function cubeChannel(level: number): number {
  return level === 0 ? 0 : CUBE_FIRST_STEP + CUBE_STEP * level;
}

/** Build the xterm 256-colour palette: the standard sixteen, a cube, then greys. */
function buildXterm256(): Rgb[] {
  const palette: Rgb[] = ANSI16_PALETTE.map((entry) => [entry.r, entry.g, entry.b] as Rgb);
  for (let ri = 0; ri < CUBE_LEVELS; ri += 1) {
    for (let gi = 0; gi < CUBE_LEVELS; gi += 1) {
      for (let bi = 0; bi < CUBE_LEVELS; bi += 1) {
        palette.push([cubeChannel(ri), cubeChannel(gi), cubeChannel(bi)]);
      }
    }
  }
  for (let index = 0; index < GREY_COUNT; index += 1) {
    const value = GREY_START + GREY_STEP * index;
    palette.push([value, value, value]);
  }
  return palette;
}

/** The xterm 256-colour palette, built once. */
export const XTERM256_PALETTE: readonly Rgb[] = buildXterm256();

/** Squared Euclidean distance between two colours. */
function distanceSquared(r: number, g: number, b: number, other: Rgb): number {
  return (r - other[0]) ** 2 + (g - other[1]) ** 2 + (b - other[2]) ** 2;
}

/**
 * The index of the nearest colour in a palette.
 *
 * The comparison is strict, so the *first* nearest wins: a colour exactly
 * between two entries takes the earlier one. Every renderer that quantises has
 * to agree on that, or the same screen renders differently in two places — and
 * the xterm palette repeats two colours the standard sixteen already have, so
 * the rule is reachable rather than theoretical.
 */
function nearestIndex(r: number, g: number, b: number, palette: readonly Rgb[]): number {
  let bestIndex = 0;
  let bestDistance = distanceSquared(r, g, b, palette[0] as Rgb);
  for (let index = 1; index < palette.length; index += 1) {
    const distance = distanceSquared(r, g, b, palette[index] as Rgb);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = index;
    }
  }
  return bestIndex;
}

/** The foreground and background codes for the nearest standard ANSI colour. */
export function nearest16(r: number, g: number, b: number): readonly [number, number] {
  const entry = ANSI16_PALETTE[
    nearestIndex(
      r,
      g,
      b,
      ANSI16_PALETTE.map((candidate) => [candidate.r, candidate.g, candidate.b] as Rgb),
    )
  ] as Ansi16Entry;
  return [entry.fg, entry.bg];
}

/** The xterm 256-colour index of the nearest colour. */
export function nearest256(r: number, g: number, b: number): number {
  return nearestIndex(r, g, b, XTERM256_PALETTE);
}
