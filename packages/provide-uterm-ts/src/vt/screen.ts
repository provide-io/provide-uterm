//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The bounded terminal screen model.
 *
 * Port of the observable behaviour of `pyte.Screen`. Only the visible
 * viewport is retained: scrolling overwrites rather than buffering, so there
 * is no off-screen scrollback at this layer and every operation is bounded by
 * `cols * rows`.
 *
 * The buffer is sparse. A cell that has never been written is absent, and
 * `display` renders it as a blank, which is what keeps a fresh screen cheap
 * and makes the golden corpus record only the cells that actually moved.
 */

import {
  BG_AIXTERM,
  BG_ANSI,
  type Char,
  defaultChar,
  FG_AIXTERM,
  FG_ANSI,
  FG_BG_256,
  isDefaultChar,
  TEXT_ATTRIBUTES,
} from "./char.ts";

/** The cursor: a position, a visibility flag, and the pending attributes. */
export interface Cursor {
  x: number;
  y: number;
  hidden: boolean;
  attrs: Char;
}

/** Distance between default tab stops. */
const TAB_WIDTH = 8;

/** Clamp `value` into `[low, high]`. */
function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/** A bounded VT/ANSI screen. */
export class Screen {
  readonly cols: number;
  readonly rows: number;
  /** Sparse row-major buffer: row index to column index to cell. */
  readonly #buffer = new Map<number, Map<number, Char>>();
  cursor: Cursor;
  #savedCursor: Cursor | undefined;
  /** Inclusive scrolling region, as row indices. */
  #marginTop = 0;
  #marginBottom: number;
  /** Insert Replacement Mode: printing shifts the rest of the line right. */
  #insertMode = false;

  constructor(cols: number, rows: number) {
    this.cols = cols;
    this.rows = rows;
    this.#marginBottom = rows - 1;
    this.cursor = { x: 0, y: 0, hidden: false, attrs: defaultChar() };
  }

  /** Each row rendered as text, with absent cells shown as blanks. */
  get display(): string[] {
    const lines: string[] = [];
    for (let y = 0; y < this.rows; y += 1) {
      const row = this.#buffer.get(y);
      let line = "";
      for (let x = 0; x < this.cols; x += 1) {
        line += row?.get(x)?.data ?? " ";
      }
      lines.push(line);
    }
    return lines;
  }

  /** The cell at `(y, x)`, or `undefined` when it was never written. */
  peek(y: number, x: number): Char | undefined {
    return this.#buffer.get(y)?.get(x);
  }

  /** Whether `char` is indistinguishable from a blank default cell. */
  isDefaultChar(char: Char): boolean {
    return isDefaultChar(char);
  }

  /** Write `char` at `(y, x)`, creating the row lazily. */
  #set(y: number, x: number, char: Char): void {
    let row = this.#buffer.get(y);
    if (row === undefined) {
      row = new Map<number, Char>();
      this.#buffer.set(y, row);
    }
    row.set(x, char);
  }

  /** The current attributes as a fresh cell carrying `data`. */
  #styled(data: string): Char {
    return { ...this.cursor.attrs, data };
  }

  // ── Printing ────────────────────────────────────────────────────────────

  /**
   * Print one character at the cursor.
   *
   * Wrapping is deferred: the cursor is allowed to rest one past the last
   * column, and the wrap happens on the *next* print. That is why a screen
   * filled to exactly its width reports a cursor at `cols` rather than
   * having already moved to the next row.
   */
  draw(data: string): void {
    if (this.cursor.x >= this.cols) {
      this.cursor.x = 0;
      this.linefeed();
    }
    if (this.#insertMode) {
      this.insertCharacters(1);
    }
    this.#set(this.cursor.y, this.cursor.x, this.#styled(data));
    this.cursor.x += 1;
  }

  // ── Cursor movement ─────────────────────────────────────────────────────

  /** Move the cursor to column zero. */
  carriageReturn(): void {
    this.cursor.x = 0;
  }

  /** Move down one row, scrolling the region when at its bottom margin. */
  linefeed(): void {
    this.index();
  }

  /** Move down one row within the scrolling region, scrolling if needed. */
  index(): void {
    if (this.cursor.y === this.#marginBottom) {
      this.#scrollUp();
      return;
    }
    this.cursor.y = Math.min(this.cursor.y + 1, this.rows - 1);
  }

  /** Move up one row within the scrolling region, scrolling if needed. */
  reverseIndex(): void {
    if (this.cursor.y === this.#marginTop) {
      this.#scrollDown();
      return;
    }
    this.cursor.y = Math.max(this.cursor.y - 1, 0);
  }

  /** Move back one column, stopping at column zero. */
  backspace(): void {
    this.cursor.x = Math.max(this.cursor.x - 1, 0);
  }

  /** Advance to the next tab stop, stopping at the last column. */
  tab(): void {
    const next = (Math.floor(this.cursor.x / TAB_WIDTH) + 1) * TAB_WIDTH;
    this.cursor.x = Math.min(next, this.cols - 1);
  }

  /** Move the cursor by `count` rows and clamp it to the screen. */
  cursorUp(count: number): void {
    this.cursor.y = Math.max(this.cursor.y - Math.max(count, 1), 0);
  }

  /** Move the cursor down by `count` rows and clamp it to the screen. */
  cursorDown(count: number): void {
    this.cursor.y = Math.min(this.cursor.y + Math.max(count, 1), this.rows - 1);
  }

  /** Move the cursor right by `count` columns and clamp it to the screen. */
  cursorForward(count: number): void {
    this.cursor.x = Math.min(this.cursor.x + Math.max(count, 1), this.cols - 1);
  }

  /** Move the cursor left by `count` columns and clamp it to the screen. */
  cursorBack(count: number): void {
    // A pending wrap is resolved before moving, so a cursor resting past the
    // last column steps back from the last column rather than from beyond it.
    this.cursor.x = Math.min(this.cursor.x, this.cols - 1);
    this.cursor.x = Math.max(this.cursor.x - Math.max(count, 1), 0);
  }

  /** Move the cursor to a one-based row and column, clamped to the screen. */
  cursorPosition(row: number, column: number): void {
    this.cursor.y = clamp(Math.max(row, 1) - 1, 0, this.rows - 1);
    this.cursor.x = clamp(Math.max(column, 1) - 1, 0, this.cols - 1);
  }

  /** Move the cursor to a one-based column on the current row. */
  cursorToColumn(column: number): void {
    this.cursor.x = clamp(Math.max(column, 1) - 1, 0, this.cols - 1);
  }

  /** Move the cursor to a one-based row in the current column. */
  cursorToLine(row: number): void {
    this.cursor.y = clamp(Math.max(row, 1) - 1, 0, this.rows - 1);
  }

  /** Save the cursor position and attributes. */
  saveCursor(): void {
    this.#savedCursor = { ...this.cursor, attrs: { ...this.cursor.attrs } };
  }

  /** Restore a saved cursor, or home if nothing was saved. */
  restoreCursor(): void {
    if (this.#savedCursor === undefined) {
      this.cursor.x = 0;
      this.cursor.y = 0;
      return;
    }
    this.cursor = { ...this.#savedCursor, attrs: { ...this.#savedCursor.attrs } };
  }

  // ── Scrolling ───────────────────────────────────────────────────────────

  /** Shift the scrolling region up one row, blanking the bottom margin. */
  #scrollUp(): void {
    for (let y = this.#marginTop; y < this.#marginBottom; y += 1) {
      this.#moveRow(y + 1, y);
    }
    this.#buffer.delete(this.#marginBottom);
  }

  /** Shift the scrolling region down one row, blanking the top margin. */
  #scrollDown(): void {
    for (let y = this.#marginBottom; y > this.#marginTop; y -= 1) {
      this.#moveRow(y - 1, y);
    }
    this.#buffer.delete(this.#marginTop);
  }

  /** Move row `from` onto row `to`, clearing `to` when `from` is absent. */
  #moveRow(from: number, to: number): void {
    const row = this.#buffer.get(from);
    if (row === undefined) {
      this.#buffer.delete(to);
      return;
    }
    this.#buffer.set(to, row);
  }

  /** Set the inclusive scrolling region from one-based rows, then home. */
  setMargins(top: number, bottom: number): void {
    const first = Math.max(top, 1) - 1;
    const last = Math.min(Math.max(bottom, 1), this.rows) - 1;
    // A region needs at least two rows; anything narrower is ignored, which
    // keeps a malformed request from wedging the screen.
    if (first >= last) {
      return;
    }
    this.#marginTop = first;
    this.#marginBottom = last;
    this.cursorPosition(1, 1);
  }

  /** Reset the scrolling region to the whole screen. */
  resetMargins(): void {
    this.#marginTop = 0;
    this.#marginBottom = this.rows - 1;
    this.cursorPosition(1, 1);
  }

  // ── Erasing ─────────────────────────────────────────────────────────────

  /** Blank the cells in `[from, to)` of row `y`. */
  #blankRange(y: number, from: number, to: number): void {
    for (let x = from; x < to; x += 1) {
      this.#set(y, x, this.#styled(" "));
    }
  }

  /**
   * Erase within the current line.
   *
   * `how` is 0 to the end of the line, 1 from its start to the cursor
   * inclusive, and 2 the whole line.
   */
  eraseInLine(how: number): void {
    if (how === 1) {
      this.#blankRange(this.cursor.y, 0, this.cursor.x + 1);
      return;
    }
    if (how === 2) {
      this.#blankRange(this.cursor.y, 0, this.cols);
      return;
    }
    this.#blankRange(this.cursor.y, this.cursor.x, this.cols);
  }

  /**
   * Erase within the screen.
   *
   * `how` is 0 to the end of the screen, 1 from its start to the cursor, and
   * 2 the whole screen.
   */
  eraseInDisplay(how: number): void {
    if (how === 1) {
      for (let y = 0; y < this.cursor.y; y += 1) {
        this.#blankRange(y, 0, this.cols);
      }
      this.#blankRange(this.cursor.y, 0, this.cursor.x + 1);
      return;
    }
    if (how === 2) {
      for (let y = 0; y < this.rows; y += 1) {
        this.#blankRange(y, 0, this.cols);
      }
      return;
    }
    this.#blankRange(this.cursor.y, this.cursor.x, this.cols);
    for (let y = this.cursor.y + 1; y < this.rows; y += 1) {
      this.#blankRange(y, 0, this.cols);
    }
  }

  /** Blank `count` cells from the cursor without shifting the rest. */
  eraseCharacters(count: number): void {
    const span = Math.max(count, 1);
    this.#blankRange(this.cursor.y, this.cursor.x, Math.min(this.cursor.x + span, this.cols));
  }

  // ── Insert and delete ───────────────────────────────────────────────────

  /** Insert `count` blank rows at the cursor, within the scrolling region. */
  insertLines(count: number): void {
    if (this.cursor.y < this.#marginTop || this.cursor.y > this.#marginBottom) {
      return;
    }
    const span = Math.max(count, 1);
    for (let i = 0; i < span; i += 1) {
      for (let y = this.#marginBottom; y > this.cursor.y; y -= 1) {
        this.#moveRow(y - 1, y);
      }
      this.#buffer.delete(this.cursor.y);
    }
    this.cursor.x = 0;
  }

  /** Delete `count` rows at the cursor, within the scrolling region. */
  deleteLines(count: number): void {
    if (this.cursor.y < this.#marginTop || this.cursor.y > this.#marginBottom) {
      return;
    }
    const span = Math.max(count, 1);
    for (let i = 0; i < span; i += 1) {
      for (let y = this.cursor.y; y < this.#marginBottom; y += 1) {
        this.#moveRow(y + 1, y);
      }
      this.#buffer.delete(this.#marginBottom);
    }
    this.cursor.x = 0;
  }

  /** Insert `count` blanks at the cursor, shifting the rest of the row right. */
  insertCharacters(count: number): void {
    const span = Math.max(count, 1);
    const row = this.#buffer.get(this.cursor.y);
    if (row !== undefined) {
      for (let x = this.cols - 1; x >= this.cursor.x + span; x -= 1) {
        const source = row.get(x - span);
        if (source === undefined) {
          row.delete(x);
        } else {
          row.set(x, source);
        }
      }
    }
    this.#blankRange(this.cursor.y, this.cursor.x, Math.min(this.cursor.x + span, this.cols));
  }

  /** Delete `count` cells at the cursor, shifting the rest of the row left. */
  deleteCharacters(count: number): void {
    const span = Math.max(count, 1);
    const row = this.#buffer.get(this.cursor.y);
    if (row !== undefined) {
      for (let x = this.cursor.x; x < this.cols; x += 1) {
        const source = row.get(x + span);
        if (source === undefined) {
          row.delete(x);
        } else {
          row.set(x, source);
        }
      }
    }
    this.#blankRange(this.cursor.y, Math.max(this.cols - span, this.cursor.x), this.cols);
  }

  // ── Modes and rendition ─────────────────────────────────────────────────

  /** Turn a mode on. `private` marks the `?`-prefixed DEC modes. */
  setMode(mode: number, isPrivate: boolean): void {
    if (isPrivate && mode === 25) {
      this.cursor.hidden = false;
      return;
    }
    if (!isPrivate && mode === 4) {
      this.#insertMode = true;
    }
  }

  /** Turn a mode off. `private` marks the `?`-prefixed DEC modes. */
  resetMode(mode: number, isPrivate: boolean): void {
    if (isPrivate && mode === 25) {
      this.cursor.hidden = true;
      return;
    }
    if (!isPrivate && mode === 4) {
      this.#insertMode = false;
    }
  }

  /**
   * Apply an SGR parameter list to the pending attributes.
   *
   * An empty list is a reset, which is why a bare `ESC [ m` clears
   * everything rather than doing nothing.
   */
  selectGraphicRendition(params: readonly number[]): void {
    const codes = params.length === 0 ? [0] : params;
    for (let i = 0; i < codes.length; i += 1) {
      const code = codes[i] as number;
      if (code === 0) {
        this.cursor.attrs = defaultChar();
        continue;
      }
      const attribute = TEXT_ATTRIBUTES[code];
      if (attribute !== undefined) {
        (this.cursor.attrs[attribute.field] as boolean) = attribute.value;
        continue;
      }
      const fg = FG_ANSI[code] ?? FG_AIXTERM[code];
      if (fg !== undefined) {
        this.cursor.attrs.fg = fg;
        continue;
      }
      const bg = BG_ANSI[code] ?? BG_AIXTERM[code];
      if (bg !== undefined) {
        this.cursor.attrs.bg = bg;
        continue;
      }
      if (code === 38 || code === 48) {
        i = this.#applyExtendedColor(codes, i, code === 38 ? "fg" : "bg");
      }
    }
  }

  /**
   * Consume an extended-colour run starting at `index`.
   *
   * Handles both the indexed form (`5;N`) and the truecolor form
   * (`2;R;G;B`), and returns the index of the last parameter consumed so the
   * caller resumes after it.
   */
  #applyExtendedColor(codes: readonly number[], index: number, field: "fg" | "bg"): number {
    const mode = codes[index + 1];
    if (mode === 5) {
      // Indexed form. A run with no index left consumes the rest, so a
      // trailing parameter is not re-read as an unrelated attribute.
      if (index + 2 >= codes.length) {
        return codes.length - 1;
      }
      const entry = FG_BG_256[codes[index + 2] as number];
      if (entry !== undefined) {
        this.cursor.attrs[field] = entry;
      }
      return index + 2;
    }
    if (mode === 2) {
      // Truecolor form, with the same rule for a short run.
      if (index + 4 >= codes.length) {
        return codes.length - 1;
      }
      const hex = [codes[index + 2], codes[index + 3], codes[index + 4]]
        .map((component) => (component as number).toString(16).padStart(2, "0"))
        .join("");
      this.cursor.attrs[field] = hex;
      return index + 4;
    }
    // An unknown mode consumes only itself, so the parameters after it are
    // still read as ordinary attributes.
    return index + 1;
  }
}
