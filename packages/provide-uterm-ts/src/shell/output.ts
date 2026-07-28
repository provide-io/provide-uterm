//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What the interactive shell writes to a terminal.
 *
 * Port of `provide.uterm.shell._output`.
 *
 * Every line ends with a carriage return as well as a newline. A terminal in
 * raw mode does not translate one into the other, so a bare newline would drop
 * a line without returning the cursor and leave the next one starting wherever
 * the last ended.
 */

/** The escape sequences the shell paints with. */
export const RESET = "\x1b[0m";
export const BOLD = "\x1b[1m";
export const DIM = "\x1b[2m";
export const GREEN = "\x1b[32m";
export const YELLOW = "\x1b[33m";
export const RED = "\x1b[31m";
export const CYAN = "\x1b[36m";
export const BLUE = "\x1b[34m";
export const MAGENTA = "\x1b[35m";

/** Erase the screen and home the cursor, in that order. */
export const CLEAR_SCREEN = "\x1b[2J\x1b[H";

/** A terminal line ending, both halves of it. */
const CRLF = "\r\n";

/** What the shell shows when it is waiting. */
export const PROMPT = `${GREEN}❯${RESET} `;

/** What it shows when it starts. */
export const BANNER =
  `${BOLD}${CYAN}ushell${RESET} ${DIM}— Python REPL inside your terminal${RESET}${CRLF}` +
  `${DIM}Type ${RESET}help${DIM} for available commands.${RESET}${CRLF}${CRLF}`;

/** How wide a key-value line's key column is unless a caller says otherwise. */
export const DEFAULT_KEY_WIDTH = 20;

/** Something went wrong. */
export function errorMsg(text: string): string {
  return `${RED}error:${RESET} ${text}${CRLF}`;
}

/** Something worth saying, quietly. */
export function infoMsg(text: string): string {
  return `${DIM}${text}${RESET}${CRLF}`;
}

/** Something went right. */
export function successMsg(text: string): string {
  return `${GREEN}${text}${RESET}${CRLF}`;
}

/** A section title. */
export function heading(text: string): string {
  return `${BOLD}${CYAN}${text}${RESET}${CRLF}`;
}

/** Pad to a width, leaving anything already wider alone. */
function padRight(text: string, width: number): string {
  return text.length >= width ? text : text + " ".repeat(width - text.length);
}

/**
 * One labelled value.
 *
 * A key wider than the column is not truncated — the value moves right
 * instead, because a key clipped in half tells a reader less than a ragged
 * column does.
 */
export function fmtKv(key: string, value: string, width: number = DEFAULT_KEY_WIDTH): string {
  return `  ${DIM}${padRight(key, width)}${RESET}${value}${CRLF}`;
}

/**
 * A fixed-width table.
 *
 * Each column is as wide as its widest cell and then at least as wide as its
 * header, computed in that order — so a header longer than any value still
 * fits, and one shorter than a value does not clip it.
 *
 * A row with fewer cells than the others truncates the whole table to its
 * length, and so does a short header list. That is the reference's behaviour,
 * which zips the rows together and stops at the shortest; a port that padded
 * instead would render a table the reference never would. Recorded rather
 * than corrected.
 */
export function fmtTable(rows: readonly (readonly string[])[], headers?: readonly string[]): string {
  if (rows.length === 0) {
    // A caller printing nothing at all leaves a user unsure whether the
    // command ran.
    return infoMsg("(no results)");
  }

  // The shortest row decides how many columns there are.
  const columns = Math.min(...rows.map((row) => row.length));
  let widths = Array.from({ length: columns }, (_value, index) =>
    Math.max(...rows.map((row) => String(row[index]).length)),
  );
  if (headers !== undefined) {
    // Zipped with the headers, so a short header list narrows the table too.
    widths = widths.slice(0, headers.length).map((width, index) => Math.max(width, (headers[index] as string).length));
  }

  const lines: string[] = [];
  if (headers !== undefined) {
    lines.push(
      `  ${widths.map((width, index) => `${BOLD}${padRight(headers[index] as string, width)}${RESET}`).join("  ")}`,
    );
    lines.push(`  ${widths.map((width) => "-".repeat(width)).join("  ")}`);
  }
  for (const row of rows) {
    lines.push(`  ${widths.map((width, index) => padRight(String(row[index]), width)).join("  ")}`);
  }
  return `${lines.join(CRLF)}${CRLF}`;
}
