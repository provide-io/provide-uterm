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

import { type Char, defaultChar, isDefaultChar } from "./char.ts";
import { CHARSET_CODES, type Charsets, initialCharsets, translate } from "./charsets.ts";
import { nfc } from "./normalize.ts";
import { applyRendition } from "./rendition.ts";
import { TabStops } from "./tab-stops.ts";
import { charWidth, isCombining } from "./width.ts";

/** The cursor: a position, a visibility flag, and the pending attributes. */
export interface Cursor {
  x: number;
  y: number;
  hidden: boolean;
  attrs: Char;
}

/** The inclusive rows of a scrolling region. */
interface Margins {
  top: number;
  bottom: number;
}

/**
 * What DECSC pushes: the cursor (pen and visibility with it), the character
 * sets, and whether origin mode and autowrap were on.
 */
interface Savepoint {
  cursor: Cursor;
  charsets: Charsets;
  origin: boolean;
  wrap: boolean;
}

/** A copy of `cursor` that shares nothing with it, its pen included. */
function copyCursor(cursor: Cursor): Cursor {
  return { ...cursor, attrs: { ...cursor.attrs } };
}

/** Clamp `value` into `[low, high]`. */
function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/** A bounded VT/ANSI screen. */
export class Screen {
  cols: number;
  rows: number;
  /** Sparse row-major buffer: row index to column index to cell. */
  readonly #buffer = new Map<number, Map<number, Char>>();
  cursor: Cursor;
  /** DECSC pushes onto this and DECRC pops it, as in the reference. */
  readonly #savepoints: Savepoint[] = [];
  /**
   * The scrolling region, or `null` for none. A region that covers the whole
   * screen is not none: origin mode refuses a row outside it, where with no
   * region the row is clamped.
   */
  #margins: Margins | null = null;
  #tabStops: TabStops;
  /** Insert Replacement Mode: printing shifts the rest of the line right. */
  #insertMode = false;
  /** DECAWM: printing past the last column wraps to the next row. */
  #autowrap = true;
  /** DECOM: row addresses count from the top of the scrolling region. */
  #originMode = false;
  /** LNM: a line feed also returns the cursor to column zero. */
  #newLineMode = false;
  /**
   * Character sets. Only a stream with `decGraphics` on ever changes them, so
   * every other screen draws its text untranslated.
   */
  #charsets: Charsets = initialCharsets();
  /**
   * Whether the stream has designated a set. G1 powers on as DEC graphics, so
   * a program that designated it so itself leaves the same sets as one that
   * never touched G1, and only the first needs a joining viewer told.
   */
  #charsetsDesignated = false;

  constructor(cols: number, rows: number) {
    this.cols = cols;
    this.rows = rows;
    this.#tabStops = new TabStops(cols);
    this.cursor = { x: 0, y: 0, hidden: false, attrs: defaultChar() };
  }

  /**
   * Each row rendered as text, with absent cells shown as blanks. The cell
   * after a wide character is left out, whatever it holds, as the reference
   * leaves it out; the empty second cell of a wide character whose first cell
   * was overwritten is drawn as nothing.
   */
  get display(): string[] {
    const lines: string[] = [];
    for (let y = 0; y < this.rows; y += 1) {
      const row = this.#buffer.get(y);
      let line = "";
      let afterWide = false;
      for (let x = 0; x < this.cols; x += 1) {
        if (afterWide) {
          afterWide = false;
          continue;
        }
        const data = row?.get(x)?.data ?? " ";
        afterWide = data !== "" && charWidth(data) === 2;
        line += data;
      }
      lines.push(line);
    }
    return lines;
  }

  /** The scrolling region, as inclusive row indices: the whole screen if none. */
  get margins(): Margins {
    return { top: this.#top, bottom: this.#bottom };
  }

  /**
   * The region's top and bottom rows, the screen's with no region. Read on
   * every line feed and cursor move, so unlike `margins` they build nothing.
   */
  get #top(): number {
    return this.#margins === null ? 0 : this.#margins.top;
  }

  get #bottom(): number {
    return this.#margins === null ? this.rows - 1 : this.#margins.bottom;
  }

  /** Whether Insert Replacement Mode is on. */
  get insertMode(): boolean {
    return this.#insertMode;
  }

  /** Whether autowrap (DECAWM) is on. */
  get autowrap(): boolean {
    return this.#autowrap;
  }

  /** Whether origin mode (DECOM) is on. */
  get originMode(): boolean {
    return this.#originMode;
  }

  /** Whether new line mode (LNM) is on. */
  get newLineMode(): boolean {
    return this.#newLineMode;
  }

  /** The G0 and G1 sets and whether G1 is shifted in, as a copy. */
  get charsets(): Charsets {
    return { ...this.#charsets };
  }

  /** Whether the stream has designated a character set since the last reset. */
  get charsetsDesignated(): boolean {
    return this.#charsetsDesignated;
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
   * Print one character at the cursor, in the cells its width takes.
   *
   * Wrapping is deferred: the cursor is allowed to rest one past the last
   * column, and the wrap happens on the *next* print. That is why a screen
   * filled to exactly its width reports a cursor at `cols` rather than
   * having already moved to the next row. With autowrap off there is no
   * wrap: the character overwrites the end of the row instead. A wide
   * character in the last column is drawn there, its second cell clipped.
   *
   * A wide character leaves its second cell holding the empty string. A
   * combining mark takes no cell: it joins the cell before the cursor, or the
   * last cell of the row above at column zero, composed (NFC).
   *
   * Returns whether the character was drawn. One with no cell and nothing to
   * combine with in the reference's terms (a control, a zero width space) is
   * not, though a deferred wrap still happens first, as in the reference;
   * the stream then drops the rest of the run of text it came in.
   */
  draw(data: string): boolean {
    const charset = this.#charsets.shifted ? this.#charsets.g1 : this.#charsets.g0;
    const shown = translate(charset, data);
    const width = charWidth(shown);
    if (this.cursor.x === this.cols) {
      if (this.#autowrap) {
        this.carriageReturn();
        this.linefeed();
      } else if (width > 0) {
        this.cursor.x -= width;
      }
    }
    if (width < 0 || (width === 0 && !isCombining(shown))) {
      return false;
    }
    if (width === 0) {
      this.#combine(shown);
      return true;
    }
    if (this.#insertMode) {
      this.insertCharacters(width);
    }
    this.#set(this.cursor.y, this.cursor.x, this.#styled(shown));
    if (width === 2 && this.cursor.x + 1 < this.cols) {
      this.#set(this.cursor.y, this.cursor.x + 1, this.#styled(""));
    }
    this.cursor.x = Math.min(this.cursor.x + width, this.cols);
    return true;
  }

  /**
   * Join a combining mark to the cell before the cursor, or at column zero to
   * the last cell of the row above. At the top left there is no such cell and
   * the mark is dropped, though it still counts as drawn.
   */
  #combine(mark: string): void {
    const [y, x] = this.cursor.x > 0 ? [this.cursor.y, this.cursor.x - 1] : [this.cursor.y - 1, this.cols - 1];
    if (y >= 0) {
      const last = this.peek(y, x) ?? defaultChar();
      this.#set(y, x, { ...last, data: nfc(last.data + mark) });
    }
  }

  // ── Character sets ──────────────────────────────────────────────────────

  /**
   * Designate `code` into G0 (`slot` is `(`) or G1 (`)`). Only `B` (ASCII),
   * `0` (DEC special graphics), `U` (IBM codepage 437) and `V` (VAX42) are
   * known; any other code is ignored.
   */
  defineCharset(code: string, slot: "(" | ")"): void {
    this.#charsetsDesignated = true;
    const charset = CHARSET_CODES.get(code);
    if (charset === undefined) {
      return;
    }
    if (slot === "(") {
      this.#charsets.g0 = charset;
    } else {
      this.#charsets.g1 = charset;
    }
  }

  /** SO: draw from G1. */
  shiftOut(): void {
    this.#charsets.shifted = true;
  }

  /** SI: draw from G0. */
  shiftIn(): void {
    this.#charsets.shifted = false;
  }

  // ── Cursor movement ─────────────────────────────────────────────────────

  /** Move the cursor to column zero. */
  carriageReturn(): void {
    this.cursor.x = 0;
  }

  /** Index, then return to column zero as well when new line mode is on. */
  linefeed(): void {
    this.index();
    if (this.#newLineMode) {
      this.carriageReturn();
    }
  }

  /**
   * Move down one row, scrolling the region when at its bottom margin.
   * Anywhere else this is a cursor down, so a cursor below the region is
   * moved up onto its bottom margin, as in the reference.
   */
  index(): void {
    if (this.cursor.y === this.#bottom) {
      this.#scrollUp();
      return;
    }
    this.cursorDown(1);
  }

  /** Move up one row, scrolling the region when at its top margin. */
  reverseIndex(): void {
    if (this.cursor.y === this.#top) {
      this.#scrollDown();
      return;
    }
    this.cursorUp(1);
  }

  /** Move back one column, stopping at column zero. */
  backspace(): void {
    this.cursorBack(1);
  }

  /**
   * Advance to the next tab stop, or to the last column when there is none.
   * A stop set beyond the last column is still a stop, as in the reference.
   */
  tab(): void {
    this.cursor.x = this.#tabStops.after(this.cursor.x) ?? this.cols - 1;
  }

  /** HTS: set a tab stop at the cursor's column. */
  setTabStop(): void {
    this.#tabStops.set(this.cursor.x);
  }

  /** TBC: `how` 0 clears the stop at the cursor, 3 clears every stop. */
  clearTabStop(how: number): void {
    if (how === 0) {
      this.#tabStops.clear(this.cursor.x);
    } else if (how === 3) {
      this.#tabStops.clearAll();
    }
  }

  /** Keep the cursor within the columns. */
  #ensureHBounds(): void {
    this.cursor.x = clamp(this.cursor.x, 0, this.cols - 1);
  }

  /**
   * Keep the cursor within the rows: within the scrolling region instead
   * when `useMargins` is set or origin mode is on, and there is a region.
   */
  #ensureVBounds(useMargins = false): void {
    const bounded = (useMargins || this.#originMode) && this.#margins !== null;
    const { top, bottom } = bounded ? (this.#margins as Margins) : { top: 0, bottom: this.rows - 1 };
    this.cursor.y = clamp(this.cursor.y, top, bottom);
  }

  /**
   * Move up `count` rows, stopping at the top margin. A cursor above the
   * region is therefore moved down onto that margin, as in the reference.
   */
  cursorUp(count: number): void {
    this.cursor.y = Math.max(this.cursor.y - Math.max(count, 1), this.#top);
  }

  /** Move down `count` rows, stopping at the bottom margin (see cursorUp). */
  cursorDown(count: number): void {
    this.cursor.y = Math.min(this.cursor.y + Math.max(count, 1), this.#bottom);
  }

  /** CPL: move up `count` rows to column zero. */
  cursorUp1(count: number): void {
    this.cursorUp(count);
    this.carriageReturn();
  }

  /** CNL: move down `count` rows to column zero. */
  cursorDown1(count: number): void {
    this.cursorDown(count);
    this.carriageReturn();
  }

  /** Move the cursor right by `count` columns and clamp it to the screen. */
  cursorForward(count: number): void {
    this.cursor.x += Math.max(count, 1);
    this.#ensureHBounds();
  }

  /** Move the cursor left by `count` columns and clamp it to the screen. */
  cursorBack(count: number): void {
    // A pending wrap is resolved before moving, so a cursor resting past the
    // last column steps back from the last column rather than from beyond it.
    if (this.cursor.x === this.cols) {
      this.cursor.x -= 1;
    }
    this.cursor.x -= Math.max(count, 1);
    this.#ensureHBounds();
  }

  /**
   * Move the cursor to a one-based row and column. In origin mode with a
   * region the row counts from the region's top, and a row outside the
   * region leaves the cursor where it is.
   */
  cursorPosition(row = 1, column = 1): void {
    let y = Math.max(row, 1) - 1;
    if (this.#margins !== null && this.#originMode) {
      y += this.#margins.top;
      if (y < this.#margins.top || y > this.#margins.bottom) {
        return;
      }
    }
    this.cursor.x = Math.max(column, 1) - 1;
    this.cursor.y = y;
    this.#ensureHBounds();
    this.#ensureVBounds();
  }

  /** Move the cursor to a one-based column on the current row. */
  cursorToColumn(column: number): void {
    this.cursor.x = Math.max(column, 1) - 1;
    this.#ensureHBounds();
  }

  /**
   * Move the cursor to a one-based row in the current column, counted from
   * the region's top in origin mode. With no region there is no offset: the
   * reference asserts a region exists there, and the Go and C# ports, like
   * this one, take the screen's top instead.
   */
  cursorToLine(row: number): void {
    this.cursor.y = Math.max(row, 1) - 1;
    if (this.#originMode) {
      this.cursor.y += this.#top;
    }
    this.#ensureVBounds();
  }

  /** DECSC: push the cursor, its pen, the character sets and two modes. */
  saveCursor(): void {
    this.#savepoints.push({
      cursor: copyCursor(this.cursor),
      charsets: { ...this.#charsets },
      origin: this.#originMode,
      wrap: this.#autowrap,
    });
  }

  /**
   * DECRC: pop the last savepoint. Origin mode and autowrap are turned back
   * on if they were on, never off, and the cursor is kept within the region.
   * With nothing saved, origin mode goes off and the cursor goes home (the
   * character sets then stay as they are).
   */
  restoreCursor(): void {
    const savepoint = this.#savepoints.pop();
    if (savepoint === undefined) {
      this.resetMode(6, true);
      this.cursorPosition();
      return;
    }
    this.#charsets = { ...savepoint.charsets };
    if (savepoint.origin) {
      this.setMode(6, true);
    }
    if (savepoint.wrap) {
      this.setMode(7, true);
    }
    this.cursor = copyCursor(savepoint.cursor);
    this.#ensureHBounds();
    this.#ensureVBounds(true);
  }

  // ── Scrolling ───────────────────────────────────────────────────────────

  /** Shift the scrolling region up one row, blanking the bottom margin. */
  #scrollUp(): void {
    const top = this.#top;
    const bottom = this.#bottom;
    for (let y = top; y < bottom; y += 1) {
      this.#moveRow(y + 1, y);
    }
    this.#buffer.delete(bottom);
  }

  /** Shift the scrolling region down one row, blanking the top margin. */
  #scrollDown(): void {
    const top = this.#top;
    const bottom = this.#bottom;
    for (let y = bottom; y > top; y -= 1) {
      this.#moveRow(y - 1, y);
    }
    this.#buffer.delete(top);
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

  /**
   * DECSTBM: set the scrolling region from one-based rows, then home. An
   * absent `bottom` keeps the current one. No rows at all, or a lone zero,
   * removes the region and leaves the cursor where it is.
   */
  setMargins(top?: number, bottom?: number): void {
    if ((top === undefined || top === 0) && bottom === undefined) {
      this.#margins = null;
      return;
    }
    const current = this.margins;
    const first = top === undefined ? current.top : clamp(top - 1, 0, this.rows - 1);
    const last = bottom === undefined ? current.bottom : clamp(bottom - 1, 0, this.rows - 1);
    // A region needs at least two rows; anything narrower is ignored, which
    // keeps a malformed request from wedging the screen.
    if (last - first < 1) {
      return;
    }
    this.#margins = { top: first, bottom: last };
    this.cursorPosition();
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
    const top = this.#top;
    const bottom = this.#bottom;
    if (this.cursor.y < top || this.cursor.y > bottom) {
      return;
    }
    const span = Math.max(count, 1);
    for (let i = 0; i < span; i += 1) {
      for (let y = bottom; y > this.cursor.y; y -= 1) {
        this.#moveRow(y - 1, y);
      }
      this.#buffer.delete(this.cursor.y);
    }
    this.cursor.x = 0;
  }

  /** Delete `count` rows at the cursor, within the scrolling region. */
  deleteLines(count: number): void {
    const top = this.#top;
    const bottom = this.#bottom;
    if (this.cursor.y < top || this.cursor.y > bottom) {
      return;
    }
    const span = Math.max(count, 1);
    for (let i = 0; i < span; i += 1) {
      for (let y = this.cursor.y; y < bottom; y += 1) {
        this.#moveRow(y + 1, y);
      }
      this.#buffer.delete(bottom);
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

  // ── Lifecycle ───────────────────────────────────────────────────────────

  /**
   * Return the screen to its initial state, keeping its dimensions. RIS
   * (`ESC c`) is this.
   *
   * A cursor saved with DECSC survives, with the sets saved beside it, so a
   * DECRC after the reset still restores it. That is the reference's reset,
   * and the Go and C# ports': it clears the screen, not the savepoints.
   */
  reset(): void {
    this.#buffer.clear();
    this.#charsets = initialCharsets();
    this.#charsetsDesignated = false;
    this.#margins = null;
    this.#tabStops = new TabStops(this.cols);
    this.#insertMode = false;
    this.#autowrap = true;
    this.#originMode = false;
    this.#newLineMode = false;
    this.cursor = { x: 0, y: 0, hidden: false, attrs: defaultChar() };
  }

  /**
   * Resize the screen.
   *
   * Rows are taken from the bottom when shrinking, so the newest output
   * survives and the oldest scrolls away, and blank rows are added at the
   * bottom when growing. Columns are clipped rather than reflowed.
   *
   * The cursor is deliberately not clamped, matching the reference: a shrink
   * can leave it beyond the last row until the next absolute move. The tab
   * stops are kept as they were, as the reference keeps them, so a wider
   * screen has none past the old width. The same size again changes nothing,
   * not even the scrolling region.
   */
  resize(rows: number, cols: number): void {
    if (rows === this.rows && cols === this.cols) {
      return;
    }
    if (rows < this.rows) {
      const dropped = this.rows - rows;
      for (let y = 0; y < rows; y += 1) {
        this.#moveRow(y + dropped, y);
      }
      for (let y = rows; y < this.rows; y += 1) {
        this.#buffer.delete(y);
      }
    }
    if (cols < this.cols) {
      for (const row of this.#buffer.values()) {
        for (let x = cols; x < this.cols; x += 1) {
          row.delete(x);
        }
      }
    }
    this.rows = rows;
    this.cols = cols;
    this.#margins = null;
  }

  // ── Modes and rendition ─────────────────────────────────────────────────

  /** Turn a mode on. `private` marks the `?`-prefixed DEC modes. */
  setMode(mode: number, isPrivate: boolean): void {
    this.#applyMode(mode, isPrivate, true);
  }

  /** Turn a mode off. `private` marks the `?`-prefixed DEC modes. */
  resetMode(mode: number, isPrivate: boolean): void {
    this.#applyMode(mode, isPrivate, false);
  }

  /**
   * Set or reset one of the modes this screen models; any other is ignored.
   * Origin mode homes the cursor either way, as the reference does.
   */
  #applyMode(mode: number, isPrivate: boolean, on: boolean): void {
    if (!isPrivate) {
      if (mode === 4) {
        this.#insertMode = on;
      } else if (mode === 20) {
        this.#newLineMode = on;
      }
      return;
    }
    if (mode === 6) {
      this.#originMode = on;
      this.cursorPosition();
    } else if (mode === 7) {
      this.#autowrap = on;
    } else if (mode === 25) {
      this.cursor.hidden = !on;
    }
  }

  /**
   * Apply an SGR parameter list to the pending attributes. See
   * {@link applyRendition}.
   */
  selectGraphicRendition(params: readonly number[]): void {
    applyRendition(this.cursor, params);
  }
}
