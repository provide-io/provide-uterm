//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The terminal state a screen snapshot has to carry besides its cells.
 *
 * A viewer that joins a live session is painted from a snapshot and then fed
 * the rest of the stream, and that stream does not start over for it: each
 * byte assumes the cursor, pen and modes the bytes before it left. A program
 * that paints with relative movement (vitetris never sends CUP) drew its whole
 * board in the wrong place on a viewer whose snapshot left the cursor at the
 * bottom-right of the painted rows, and in the default background on one that
 * never saw its last SGR.
 *
 * `screenStateSuffix` follows the painted rows and leaves a fresh terminal in
 * the screen's state. Port of the Python module
 * `provide.uterm.render.terminal_state`, emitting the same bytes for the
 * state this screen model keeps: the scroll region, origin mode, the cursor
 * (the deferred wrap at the last column included), autowrap, insert and new
 * line modes, the G0/G1 sets and shift, the pen and cursor visibility. The
 * input modes are not modelled here, so their reset values are sent, as the
 * reference sends for a screen that never set them.
 */

import { type Char, type Charset, charWidth, type Screen } from "../vt/index.ts";
import {
  ANSI_HIDE_CURSOR,
  ANSI_RESET,
  ANSI_SHOW_CURSOR,
  cellStyle,
  colorSgr,
  isSecondHalf,
  styleToSgr,
} from "./sgr.ts";

/** SGR parameter for each pen flag, in emission order. */
const PEN_FLAGS: ReadonlyArray<[keyof Char, number]> = [
  ["bold", 1],
  ["italics", 3],
  ["underscore", 4],
  ["blink", 5],
  ["reverse", 7],
  ["strikethrough", 9],
];

/** The designator for each set a slot can hold. */
const CHARSET_CODES: Readonly<Record<Charset, string>> = { ascii: "B", dec: "0", ibmpc: "U", vax42: "V" };

/**
 * The SGR sequence that sets a fresh terminal's pen to `attrs`: from a reset,
 * every flag, then both colours. Reverse stays reverse.
 */
export function penSgr(attrs: Char): string {
  const codes = [0];
  for (const [field, code] of PEN_FLAGS) {
    if (attrs[field]) {
      codes.push(code);
    }
  }
  codes.push(...colorSgr(attrs.fg === "" ? "default" : attrs.fg, true));
  codes.push(...colorSgr(attrs.bg === "" ? "default" : attrs.bg, false));
  return `\x1b[${codes.join(";")}m`;
}

/**
 * The column and bytes that redraw row `y`'s last cell, leaving the cursor
 * deferred: `<column>H<pen><text>`, for a caller to prefix with `ESC [ <row> ;`.
 *
 * The cell is drawn again, in its own style, from the column before; a wide
 * character there occupies the last two cells and is redrawn whole. Marks in
 * the cell, or in a wide character's second cell, are written after it with
 * autowrap off: they join the cell before the cursor, which rests past the
 * last column, and the suffix has already turned autowrap on, so they would
 * otherwise wrap to the next row first.
 *
 * A last cell holding a second half whose first half is gone, or the second
 * half of a wide character with marks of its own, cannot be drawn that way:
 * with `exact` this returns `undefined` for it, and otherwise the nearest
 * thing. Port of `_last_cell_redraw`.
 */
function lastCellRedraw(screen: Screen, y: number, exact: boolean): string | undefined {
  // An absent cell is a blank, as the reference's default cell is.
  const dataAt = (x: number): string => screen.peek(y, x)?.data ?? " ";
  let column = screen.cols - 1;
  let marks = "";
  const last = dataAt(column);
  if (column > 0 && isSecondHalf(last)) {
    const before = dataAt(column - 1);
    if (exact && !([...before].length === 1 && charWidth(before) === 2)) {
      return undefined;
    }
    marks = last;
    column -= 1;
  } else if (exact && isSecondHalf(last)) {
    return undefined;
  }
  // A row scrolled away under a resting cursor leaves no cell: a blank.
  const cell = screen.peek(y, column);
  const sgr = cell === undefined ? ANSI_RESET : styleToSgr(cellStyle(cell));
  const text = cell === undefined || cell.data === "" ? " " : cell.data;
  const base = String.fromCodePoint(text.codePointAt(0) as number);
  const held = text.slice(base.length) + marks;
  const redraw = `${column + 1}H${sgr}${base}`;
  return held === "" ? redraw : `${redraw}\x1b[?7l${held}\x1b[?7h`;
}

/**
 * Move to the cursor, reproducing the deferred wrap at the last column.
 *
 * A cursor that has just drawn into the last column rests at `x === cols`,
 * which no CUP can address, so the row's last cell is drawn again (see
 * {@link lastCellRedraw}), which leaves any terminal in the same state.
 *
 * Some last cells cannot be drawn so: a second half whose first half is gone,
 * or one of a wide character with marks of its own. A cursor gets there
 * deferred when CUU, CUD, IND or RI move it from another row, all of which
 * keep the column. So that is done here too: the cursor is left deferred past
 * a neighbouring row's last cell, redrawn as it is, and moved onto this row
 * with CUD from the row above or CUU from the row below. Either must land on
 * this row despite the scrolling region, and in origin mode the neighbour
 * must be inside it. Only with no such neighbour (a one-row screen, or
 * neighbours that cannot be drawn either) is the last cell drawn as nearly as
 * it can be. Port of `_cursor_placement`.
 */
function cursorPlacement(screen: Screen, row: number): string {
  const { x, y } = screen.cursor;
  if (x < screen.cols) {
    return `\x1b[${row};${x + 1}H`;
  }
  const own = lastCellRedraw(screen, y, true);
  if (own !== undefined) {
    return `\x1b[${row};${own}`;
  }
  const { top, bottom } = screen.margins;
  const neighbours: Array<[number, string, boolean]> = [
    [y - 1, "\x1b[B", y <= bottom],
    [y + 1, "\x1b[A", y >= top],
  ];
  for (const [neighbour, move, lands] of neighbours) {
    if (!lands || neighbour < 0 || neighbour >= screen.rows) {
      continue;
    }
    if (screen.originMode && (neighbour < top || neighbour > bottom)) {
      continue;
    }
    const redraw = lastCellRedraw(screen, neighbour, true);
    if (redraw !== undefined) {
      return `\x1b[${row + neighbour - y};${redraw}${move}`;
    }
  }
  return `\x1b[${row};${lastCellRedraw(screen, y, false) as string}`;
}

/**
 * The designations and shift, unless nothing has touched them. The power-on
 * sets are sent once the stream has designated any: a viewer's G1 powers on
 * as ASCII, so a program that designated G1 as graphics before it joined
 * would otherwise draw letters at its next SO. Untouched, nothing is sent,
 * because a snapshot is also read as text and `strip_ansi` leaves
 * designations in.
 */
function charsetState(screen: Screen): string {
  const { g0, g1, shifted } = screen.charsets;
  if (g0 === "ascii" && g1 === "dec" && !shifted && !screen.charsetsDesignated) {
    return "";
  }
  return `\x1b(${CHARSET_CODES[g0]}\x1b)${CHARSET_CODES[g1]}${shifted ? "\x0e" : ""}`;
}

/**
 * What must follow the painted rows to leave a terminal in `screen`'s state.
 *
 * `charsets` says whether the stream honours designations and SO/SI at all:
 * a CP437 session ignores them, so it has no such state to restore. The
 * order is the reference's: DECSTBM and DECOM both home the cursor, so they
 * come first; the deferred-wrap redraw needs autowrap on and insert mode off,
 * so the real values of those follow it. In origin mode the cursor's row is
 * addressed from the top of the region.
 */
export function screenStateSuffix(screen: Screen, options: { charsets: boolean }): string {
  const { top, bottom } = screen.margins;
  // A region covering the whole screen is sent as the reset, as the reference
  // sends it, so that "none" and "all of it" are the same bytes.
  const whole = top === 0 && bottom === screen.rows - 1;
  const origin = screen.originMode;
  const parts = [whole ? "\x1b[r" : `\x1b[${top + 1};${bottom + 1}r`];
  parts.push(origin ? "\x1b[?6h" : "\x1b[?6l", "\x1b[?7h\x1b[4l");
  parts.push(cursorPlacement(screen, screen.cursor.y + 1 - (origin ? top : 0)));
  if (!screen.autowrap) {
    parts.push("\x1b[?7l");
  }
  if (screen.insertMode) {
    parts.push("\x1b[4h");
  }
  parts.push(screen.newLineMode ? "\x1b[20h" : "\x1b[20l", "\x1b[?1l");
  if (options.charsets) {
    parts.push(charsetState(screen));
  }
  parts.push(penSgr(screen.cursor.attrs), screen.cursor.hidden ? ANSI_HIDE_CURSOR : ANSI_SHOW_CURSOR);
  return parts.join("");
}
