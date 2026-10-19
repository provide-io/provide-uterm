//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The character sets a G0 or G1 slot can hold, and what each draws.
 *
 * The 256-entry tables for `ESC ( U` and `ESC ( V` are generated from the
 * reference's (./charset-tables.ts); the vt golden corpus exports them too,
 * and the tests compare every entry against it.
 */

import { IBMPC_MAP, VAX42_MAP } from "./charset-tables.ts";

/**
 * A character set a G0 or G1 slot can hold: ASCII, DEC special graphics, IBM
 * codepage 437 or VAX42, the four the reference knows.
 */
export type Charset = "ascii" | "dec" | "ibmpc" | "vax42";

/**
 * DEC special graphics: what each redrawn character becomes, keyed by the
 * ASCII it replaces. The table pyte draws (and the Go port's `vt100Map`), so
 * it carries the arrows and block at `+,-.0` as well as the 0x5F-0x7E set.
 */
const DEC_SPECIAL_GRAPHICS: ReadonlyMap<string, string> = new Map(
  Object.entries({
    "+": "\u2192",
    ",": "\u2190",
    "-": "\u2191",
    ".": "\u2193",
    "0": "\u2588",
    _: "\u00a0",
    "`": "\u25c6",
    a: "\u2592",
    b: "\u2409",
    c: "\u240c",
    d: "\u240d",
    e: "\u240a",
    f: "\u00b0",
    g: "\u00b1",
    h: "\u2591",
    i: "\u240b",
    j: "\u2518",
    k: "\u2510",
    l: "\u250c",
    m: "\u2514",
    n: "\u253c",
    o: "\u23ba",
    p: "\u23bb",
    q: "\u2500",
    r: "\u23bc",
    s: "\u23bd",
    t: "\u251c",
    u: "\u2524",
    v: "\u2534",
    w: "\u252c",
    x: "\u2502",
    y: "\u2264",
    z: "\u2265",
    "{": "\u03c0",
    "|": "\u2260",
    "}": "\u00a3",
    "~": "\u00b7",
  }),
);

/** The designator codes a G0 or G1 slot accepts. Any other code is ignored. */
export const CHARSET_CODES: ReadonlyMap<string, Charset> = new Map([
  ["B", "ascii"],
  ["0", "dec"],
  ["U", "ibmpc"],
  ["V", "vax42"],
]);

/** The 256-entry tables of the sets that redraw by code point. */
const CHARSET_TABLES: Readonly<Record<"ibmpc" | "vax42", readonly number[]>> = { ibmpc: IBMPC_MAP, vax42: VAX42_MAP };

/**
 * What `data` is drawn as from `charset`. The reference translates only code
 * points below 256, so anything above them is drawn as it is in every set.
 */
export function translate(charset: Charset, data: string): string {
  if (charset === "ascii") {
    return data;
  }
  if (charset === "dec") {
    return DEC_SPECIAL_GRAPHICS.get(data) ?? data;
  }
  const point = data.codePointAt(0) as number;
  return point < 256 ? String.fromCodePoint(CHARSET_TABLES[charset][point] as number) : data;
}

/** The G0 and G1 sets and which one is shifted in. */
export interface Charsets {
  g0: Charset;
  g1: Charset;
  shifted: boolean;
}

/**
 * The reference's power-on sets: ASCII in G0, DEC special graphics in G1.
 * That is pyte and the Linux console, not xterm (ASCII in both), and it is
 * what the Python emulator draws: an undesignated SO draws lines here.
 */
export function initialCharsets(): Charsets {
  return { g0: "ascii", g1: "dec", shifted: false };
}
