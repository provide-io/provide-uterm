//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Painting a `snapshot` frame into a terminal that then resumes the live stream.
 *
 * The live bytes after a snapshot assume the cursor is where the program left
 * it. A program that paints with relative movement (CUU/CUD/CUF, CR, LF; never
 * CUP) draws everything in the wrong place on a terminal whose cursor sits
 * where the painted rows happened to end.
 *
 * An emulator's ANSI screen carries that state itself: it ends with the cursor
 * position, pen and modes (see `screen_state_suffix` in the core package). A
 * plain-text screen -- the bridges' snapshots are `get_snapshot()["screen"]`,
 * rows of characters and nothing else -- cannot, and there the frame's
 * `cursor` field is the only source. So the cursor is placed from the frame
 * exactly when the screen has no escape sequences in it: a screen with any
 * either positions the cursor itself or was never an emulated screen (a raw
 * output replay, which leaves its own cursor), and moving it would undo that.
 */

/** Reset, clear and home: the state a snapshot is painted from. */
export const SNAPSHOT_PREAMBLE = "\x1b[!p\x1b[2J\x1b[H";

/** A snapshot's rows as terminal output: rows are joined with a bare LF. */
export function snapshotRows(screen: string): string {
  return screen.replace(/\n/g, "\r\n");
}

function cursorCoordinate(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;
}

/**
 * The CUP that leaves the cursor where a plain-text snapshot says it is, or
 * `""` when the screen positions it itself or the frame names no cursor.
 *
 * `x` may equal the width: an emulator records a cursor that has just drawn
 * into the last column that way. CUP cannot address the deferred wrap, so the
 * terminal clamps it to the last column, which is the nearest it can get.
 */
export function snapshotCursorRestore(screen: string, cursor: unknown): string {
  if (screen.includes("\x1b") || typeof cursor !== "object" || cursor === null) {
    return "";
  }
  const { x, y } = cursor as { x?: unknown; y?: unknown };
  const column = cursorCoordinate(x);
  const row = cursorCoordinate(y);
  if (column === undefined || row === undefined) {
    return "";
  }
  return `\x1b[${row + 1};${column + 1}H`;
}
