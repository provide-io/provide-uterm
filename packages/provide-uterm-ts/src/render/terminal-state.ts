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
 * state this screen model keeps: the scroll region, the cursor (the deferred
 * wrap at the last column included), insert mode, the G0/G1 sets and shift,
 * the pen and cursor visibility. Origin mode, autowrap, new-line mode and the
 * input modes are not modelled here, so their reset values are sent, as the
 * reference sends for a screen that never set them.
 */

import type { Char, Charset, Screen } from "../vt/index.ts";
import { ANSI_HIDE_CURSOR, ANSI_RESET, ANSI_SHOW_CURSOR, cellStyle, colorSgr, styleToSgr } from "./sgr.ts";

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
const CHARSET_CODES: Readonly<Record<Charset, string>> = { ascii: "B", dec: "0" };

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
 * Move to the cursor, reproducing the deferred wrap at the last column.
 *
 * A cursor that has just drawn into the last column rests at `x === cols`,
 * which no CUP can address. The last cell is drawn again, in its own style,
 * from the column before, which leaves any terminal in the same state.
 */
function cursorPlacement(screen: Screen): string {
  const { x, y } = screen.cursor;
  if (x < screen.cols) {
    return `\x1b[${y + 1};${x + 1}H`;
  }
  // A row scrolled away under a resting cursor leaves no cell: a blank.
  const cell = screen.peek(y, screen.cols - 1);
  const sgr = cell === undefined ? ANSI_RESET : styleToSgr(cellStyle(cell));
  const data = cell === undefined || cell.data === "" ? " " : cell.data;
  return `\x1b[${y + 1};${screen.cols}H${sgr}${data}`;
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
 * order is the reference's: DECSTBM homes the cursor, so it comes first; the
 * deferred-wrap redraw needs insert mode off, so the real value follows it.
 */
export function screenStateSuffix(screen: Screen, options: { charsets: boolean }): string {
  const { top, bottom } = screen.margins;
  const parts = [top === 0 && bottom === screen.rows - 1 ? "\x1b[r" : `\x1b[${top + 1};${bottom + 1}r`];
  parts.push("\x1b[?6l", "\x1b[?7h\x1b[4l", cursorPlacement(screen));
  if (screen.insertMode) {
    parts.push("\x1b[4h");
  }
  parts.push("\x1b[20l", "\x1b[?1l");
  if (options.charsets) {
    parts.push(charsetState(screen));
  }
  parts.push(penSgr(screen.cursor.attrs), screen.cursor.hidden ? ANSI_HIDE_CURSOR : ANSI_SHOW_CURSOR);
  return parts.join("");
}
