//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Per-cell SGR rendering.
 *
 * Turns a screen's style buffer back into ANSI-styled text, emitting an
 * escape only where the style changes between adjacent cells. Used wherever a
 * consumer needs the *visual* state including colour, which a plain-text
 * snapshot discards.
 *
 * Port of the render half of the Python module
 * `provide.uterm.render.buffer` and the Go package `render`.
 */

import type { Char } from "../vt/index.ts";

/** Reset every attribute. */
export const ANSI_RESET = "\x1b[0m";
/** Hide the cursor. */
export const ANSI_HIDE_CURSOR = "\x1b[?25l";
/** Show the cursor. */
export const ANSI_SHOW_CURSOR = "\x1b[?25h";
/** Switch to the alternate screen buffer. */
export const ANSI_ALT_SCREEN = "\x1b[?1049h";
/** Leave the alternate screen buffer. */
export const ANSI_EXIT_ALT = "\x1b[?1049l";

/** The CSI sequence positioning the cursor at a one-based row and column. */
export function moveTo(row: number, col: number): string {
  return `\x1b[${row};${col}H`;
}

/** The CSI sequence erasing the whole screen. */
export function clearScreen(): string {
  return "\x1b[2J";
}

/**
 * Foreground SGR codes by colour name.
 *
 * Note what is *absent*: the screen model names its yellow `brown`, and this
 * table does not carry that name, so a brown cell emits no colour code at
 * all. That is the reference's behaviour and the corpus pins it rather than
 * quietly correcting it — correcting it here would change the bytes a
 * consumer receives.
 */
export const FG_CODES: Readonly<Record<string, number>> = {
  black: 30,
  red: 31,
  green: 32,
  yellow: 33,
  blue: 34,
  magenta: 35,
  cyan: 36,
  white: 37,
  brightblack: 90,
  brightred: 91,
  brightgreen: 92,
  brightyellow: 93,
  brightblue: 94,
  brightmagenta: 95,
  brightcyan: 96,
  brightwhite: 97,
};

/** Background SGR codes by colour name, with the same `brown` gap. */
export const BG_CODES: Readonly<Record<string, number>> = {
  black: 40,
  red: 41,
  green: 42,
  yellow: 43,
  blue: 44,
  magenta: 45,
  cyan: 46,
  white: 47,
  brightblack: 100,
  brightred: 101,
  brightgreen: 102,
  brightyellow: 103,
  brightblue: 104,
  brightmagenta: 105,
  brightcyan: 106,
  brightwhite: 107,
};

/** Whether `value` is a six-character hex RGB string. */
function isHexColor(value: string): boolean {
  return /^[0-9a-fA-F]{6}$/.test(value);
}

/** SGR codes for one colour value, which may be a name or a hex triple. */
function colorSgr(color: string, isForeground: boolean): number[] {
  if (color === "default") {
    return [];
  }
  const table = isForeground ? FG_CODES : BG_CODES;
  const named = table[color];
  if (named !== undefined) {
    return [named];
  }
  if (isHexColor(color)) {
    const base = isForeground ? 38 : 48;
    return [
      base,
      2,
      Number.parseInt(color.slice(0, 2), 16),
      Number.parseInt(color.slice(2, 4), 16),
      Number.parseInt(color.slice(4, 6), 16),
    ];
  }
  return [];
}

/** SGR codes for the boolean attributes the renderer emits. */
function attrCodes(bold: boolean, underscore: boolean, blink: boolean): number[] {
  const codes: number[] = [];
  if (bold) {
    codes.push(1);
  }
  if (underscore) {
    codes.push(4);
  }
  if (blink) {
    codes.push(5);
  }
  return codes;
}

/** The style attributes the renderer distinguishes between adjacent cells. */
export interface CellStyle {
  fg: string;
  bg: string;
  bold: boolean;
  underscore: boolean;
  reverse: boolean;
  blink: boolean;
}

/**
 * Convert cell style attributes to an SGR escape sequence.
 *
 * Reverse video is applied by swapping the two colours before they are
 * encoded rather than by emitting SGR 7, so a consumer that only understands
 * colours still sees the inversion. A style with nothing to say emits a plain
 * reset.
 */
export function styleToSgr(style: CellStyle): string {
  const fg = style.reverse ? style.bg : style.fg;
  const bg = style.reverse ? style.fg : style.bg;
  const codes = [
    ...attrCodes(style.bold, style.underscore, style.blink),
    ...colorSgr(fg, true),
    ...colorSgr(bg, false),
  ];
  if (codes.length === 0) {
    return ANSI_RESET;
  }
  return `\x1b[${codes.join(";")}m`;
}

/** The style of an absent cell. */
const DEFAULT_STYLE: CellStyle = {
  fg: "default",
  bg: "default",
  bold: false,
  underscore: false,
  reverse: false,
  blink: false,
};

/** Whether two styles would render identically. */
function sameStyle(a: CellStyle, b: CellStyle): boolean {
  return (
    a.fg === b.fg &&
    a.bg === b.bg &&
    a.bold === b.bold &&
    a.underscore === b.underscore &&
    a.reverse === b.reverse &&
    a.blink === b.blink
  );
}

/** Reads one cell of a screen buffer. */
export type CellReader = (y: number, x: number) => Char | undefined;

/**
 * Render a screen's cells to ANSI-styled row strings.
 *
 * An escape is emitted only where the style changes between adjacent cells,
 * and each row ends with a reset so a consumer's next write starts clean.
 */
export function renderCellRows(read: CellReader, cols: number, rows: number): string[] {
  const lines: string[] = [];
  for (let y = 0; y < rows; y += 1) {
    const parts: string[] = [];
    let lastStyle: CellStyle | undefined;
    for (let x = 0; x < cols; x += 1) {
      const cell = read(y, x);
      const style: CellStyle =
        cell === undefined
          ? DEFAULT_STYLE
          : {
              fg: cell.fg === "" ? "default" : cell.fg,
              bg: cell.bg === "" ? "default" : cell.bg,
              bold: cell.bold,
              underscore: cell.underscore,
              reverse: cell.reverse,
              blink: cell.blink,
            };
      if (lastStyle === undefined || !sameStyle(style, lastStyle)) {
        parts.push(styleToSgr(style));
        lastStyle = style;
      }
      parts.push(cell === undefined || cell.data === "" ? " " : cell.data);
    }
    parts.push(ANSI_RESET);
    lines.push(parts.join(""));
  }
  return lines;
}
