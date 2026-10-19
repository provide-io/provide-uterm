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

import { type Char, charWidth } from "../vt/index.ts";

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
 * The screen model names its yellow `brown` (as pyte does), so both names are
 * here. Without `brown` a yellow cell emitted no colour code at all, and a
 * snapshot painted it in the default colour.
 */
export const FG_CODES: Readonly<Record<string, number>> = {
  black: 30,
  red: 31,
  green: 32,
  yellow: 33,
  brown: 33,
  blue: 34,
  magenta: 35,
  cyan: 36,
  white: 37,
  brightblack: 90,
  brightred: 91,
  brightgreen: 92,
  brightyellow: 93,
  brightbrown: 93,
  brightblue: 94,
  brightmagenta: 95,
  brightcyan: 96,
  brightwhite: 97,
};

/** Background SGR codes by colour name, `brown` included. */
export const BG_CODES: Readonly<Record<string, number>> = {
  black: 40,
  red: 41,
  green: 42,
  yellow: 43,
  brown: 43,
  blue: 44,
  magenta: 45,
  cyan: 46,
  white: 47,
  brightblack: 100,
  brightred: 101,
  brightgreen: 102,
  brightyellow: 103,
  brightbrown: 103,
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
export function colorSgr(color: string, isForeground: boolean): number[] {
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
function attrCodes(bold: boolean, underscore: boolean, blink: boolean, italics: boolean): number[] {
  const codes: number[] = [];
  if (bold) {
    codes.push(1);
  }
  if (italics) {
    codes.push(3);
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
  italics: boolean;
  strikethrough: boolean;
}

/**
 * Convert cell style attributes to an SGR escape sequence.
 *
 * The sequence is absolute: it starts from a reset, so it sets exactly this
 * style whatever the previous cell's was. Relative to the previous style, a
 * cell that dropped an attribute kept it -- a background bleeding along the
 * row. Reverse video is SGR 7: swapping the colours instead is no change at
 * all when both are the default, and the reversed cell vanished. A style with
 * nothing to say emits a plain reset. The attributes go in the order the pen's
 * do (1, 3, 4, 5, 7, 9).
 */
export function styleToSgr(style: CellStyle): string {
  const codes = [
    0,
    ...attrCodes(style.bold, style.underscore, style.blink, style.italics),
    ...(style.reverse ? [7] : []),
    ...(style.strikethrough ? [9] : []),
    ...colorSgr(style.fg, true),
    ...colorSgr(style.bg, false),
  ];
  if (codes.length === 1) {
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
  italics: false,
  strikethrough: false,
};

/** The style the renderer reads from a cell. */
export function cellStyle(cell: Char): CellStyle {
  return {
    fg: cell.fg === "" ? "default" : cell.fg,
    bg: cell.bg === "" ? "default" : cell.bg,
    bold: cell.bold,
    underscore: cell.underscore,
    reverse: cell.reverse,
    blink: cell.blink,
    italics: cell.italics,
    strikethrough: cell.strikethrough,
  };
}

/** Whether two styles would render identically. */
function sameStyle(a: CellStyle, b: CellStyle): boolean {
  return (
    a.fg === b.fg &&
    a.bg === b.bg &&
    a.bold === b.bold &&
    a.underscore === b.underscore &&
    a.reverse === b.reverse &&
    a.blink === b.blink &&
    a.italics === b.italics &&
    a.strikethrough === b.strikethrough
  );
}

/** Reads one cell of a screen buffer. */
export type CellReader = (y: number, x: number) => Char | undefined;

/**
 * Drawn where a cell holds the empty second half of a wide character whose
 * first half is gone, to recreate that cell: see {@link renderCellRow}. Any
 * wide character would do; this one is blank should a terminal ever show it.
 */
const PLACEHOLDER = "\u3000";

/** Whether a cell holds what a wide character's second cell does: nothing, or marks. */
export function isSecondHalf(text: string): boolean {
  return text === "" || charWidth(text) === 0;
}

/** The text and style of a cell, an absent one being a blank default cell. */
function cellAt(read: CellReader, y: number, x: number): [string, CellStyle] {
  const cell = read(y, x);
  return cell === undefined ? [" ", DEFAULT_STYLE] : [cell.data, cellStyle(cell)];
}

/**
 * Render row `y` so that drawing it on a fresh row recreates every cell.
 *
 * A wide character is drawn once and takes two cells; the cell after it is
 * recreated by drawing it, so nothing is written for that cell. Writing a
 * space there, as this once did, made every wide character a column wider on
 * the terminal it was drawn on, and a row holding one overflowed. Combining
 * marks that joined that second cell are written straight after the wide
 * character, which joins them to it again; marks joined to the wide character
 * itself are written with the column set back onto its second cell, which
 * joins them to the wide character instead.
 *
 * Two cells cannot be recreated by drawing left to right, and get a cursor
 * move. A wide character whose second half was overwritten is drawn, and the
 * column is then set back onto that half (CHA, not CUB: from the deferred
 * wrap at the last column CUB steps one further). A second half whose first
 * half is gone (overwritten, or deleted) is recreated the way the stream made
 * it: a placeholder wide character drawn in its style, its marks joined to
 * it, and the placeholder's first half deleted (DCH). In the last column,
 * where a wide character has no second half, the placeholder is drawn one
 * column left instead, and the cell there drawn again over its first half.
 * That needs the cell before it to be drawable, so one arrangement is left
 * blank: such a cell in the last column right after another second half (a
 * wide character's, or another such cell). Any marks in it are lost with it.
 *
 * A cell with marks written into the last column is written with autowrap
 * off: the marks join the cell before the cursor, which then rests past the
 * last column, and with autowrap on they would first wrap to the next row.
 *
 * Port of the Python `_render_row`; every port writes the same bytes.
 */
function renderCellRow(read: CellReader, y: number, cols: number): string {
  const parts: string[] = [];
  let lastStyle: CellStyle | undefined;
  const put = (text: string, style: CellStyle): void => {
    if (lastStyle === undefined || !sameStyle(style, lastStyle)) {
      parts.push(styleToSgr(style));
      lastStyle = style;
    }
    parts.push(text);
  };
  const putCell = (text: string, style: CellStyle, x: number): void => {
    if (text === "") {
      return;
    }
    if (x === cols - 1 && ([...text].length > 1 || charWidth(text) === 0)) {
      parts.push("\x1b[?7l");
      put(text, style);
      parts.push("\x1b[?7h");
    } else {
      put(text, style);
    }
  };

  let x = 0;
  while (x < cols) {
    const [data, style] = cellAt(read, y, x);
    if (isSecondHalf(data)) {
      if (x + 1 < cols) {
        put(PLACEHOLDER, style);
        putCell(data, style, x + 1);
        parts.push(`\x1b[${x + 1}G\x1b[P\x1b[C`);
      } else if (x > 0) {
        const [before, beforeStyle] = cellAt(read, y, x - 1);
        if (!isSecondHalf(before)) {
          parts.push(`\x1b[${x}G`);
          put(PLACEHOLDER, style);
          putCell(data, style, x);
          parts.push(`\x1b[${x}G`);
          putCell(before, beforeStyle, x - 1);
        }
      }
      x += 1;
      continue;
    }
    if (x + 1 === cols || charWidth(data) !== 2) {
      putCell(data, style, x);
      x += 1;
      continue;
    }
    // A wide character with a cell after it. Its own marks are written with
    // the cursor back on that cell, which joins them to the wide character
    // rather than to it.
    const base = String.fromCodePoint(data.codePointAt(0) as number);
    const own = data.slice(base.length);
    put(base, style);
    const [after, afterStyle] = cellAt(read, y, x + 1);
    if (isSecondHalf(after)) {
      putCell(after, afterStyle, x + 1);
      if (own !== "") {
        parts.push(`\x1b[${x + 2}G`);
        put(own, style);
        if (x + 2 < cols) {
          parts.push(`\x1b[${x + 3}G`);
        }
      }
      x += 2;
    } else {
      parts.push(`\x1b[${x + 2}G`);
      if (own !== "") {
        put(own, style);
      }
      x += 1;
    }
  }
  parts.push(ANSI_RESET);
  return parts.join("");
}

/**
 * Render a screen's cells to ANSI-styled row strings.
 *
 * An escape is emitted only where the style changes between adjacent cells,
 * and each row ends with a reset so a consumer's next write starts clean.
 * Drawn on fresh rows, the rows recreate the cells: see {@link renderCellRow}.
 */
export function renderCellRows(read: CellReader, cols: number, rows: number): string[] {
  const lines: string[] = [];
  for (let y = 0; y < rows; y += 1) {
    lines.push(renderCellRow(read, y, cols));
  }
  return lines;
}
