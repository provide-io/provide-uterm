//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * VT/ANSI terminal emulation over the `vt` screen model.
 *
 * Port of the Python module `provide.uterm.emulator` and the Go package
 * `emulator`.
 *
 * Memory bounds: the screen is bounded to its viewport, so scrolling
 * overwrites rather than buffering and there is no off-screen scrollback
 * here. Applications needing history record the raw byte stream separately.
 * A bounded rolling tail of raw decoded output is kept so a consumer can
 * still recover what scrolled off within a single turn.
 */

import { createHash } from "node:crypto";
import { renderCellRows } from "../render/index.ts";
import { decodeCp437 } from "../screen/index.ts";
import { Screen, Stream } from "../vt/index.ts";

/**
 * Bounded rolling raw-output tail, in decoded characters with ANSI and
 * control sequences intact. The screen keeps only its viewport, so output
 * that scrolls off within a single server turn is otherwise unrecoverable.
 * Kept small so it rides every snapshot frame cheaply.
 */
const RAW_TAIL_MAX = 4096;

/** Construction options for {@link TerminalEmulator}. */
export interface TerminalEmulatorOptions {
  /** Width in columns. Defaults to 80. */
  cols?: number;
  /** Height in rows. Defaults to 25. */
  rows?: number;
  /** Terminal type string. Defaults to `"ANSI"`. */
  term?: string;
  /**
   * Codec for incoming terminal bytes. Defaults to CP437 for byte-oriented
   * BBS compatibility. Only `cp437` and `utf-8` are recognised.
   */
  receiveEncoding?: string;
}

/** The screen state a snapshot reports. */
export interface Snapshot {
  screen: string;
  screen_hash: string;
  cursor: { x: number; y: number };
  cols: number;
  rows: number;
  term: string;
  cursor_at_end: boolean;
  has_trailing_space: boolean;
  raw_tail: string;
  captured_at: number;
}

/** A VT/ANSI terminal emulator. */
export class TerminalEmulator {
  cols: number;
  rows: number;
  readonly term: string;
  readonly receiveEncoding: string;

  readonly #screen: Screen;
  readonly #stream: Stream;
  #dirty = true;
  #cached: Omit<Snapshot, "captured_at"> | undefined;
  #rawTail = "";

  constructor(options: TerminalEmulatorOptions = {}) {
    this.cols = options.cols ?? 80;
    this.rows = options.rows ?? 25;
    this.term = options.term ?? "ANSI";
    this.receiveEncoding = options.receiveEncoding ?? "cp437";
    this.#screen = new Screen(this.cols, this.rows);
    this.#stream = new Stream(this.#screen);
  }

  /** Decode and feed raw terminal bytes through the emulator. */
  process(data: Uint8Array): void {
    const text = this.receiveEncoding === "cp437" ? decodeCp437(data) : Buffer.from(data).toString("utf-8");
    this.#stream.feed(text);
    if (text !== "") {
      this.#rawTail = (this.#rawTail + text).slice(-RAW_TAIL_MAX);
    }
    this.#dirty = true;
  }

  /** The bounded rolling tail of raw decoded output, ANSI intact. */
  getRawTail(): string {
    return this.#rawTail;
  }

  /**
   * Whether the cursor sits at or past the end of the last content line.
   *
   * The two-character slack is deliberate rather than an off-by-one: BBS
   * prompts often leave a trailing space or caret after the input point
   * (`"> "` or `"> _"`), and a detector needs those read as "still at the
   * prompt" rather than "the user has typed". A tighter check misclassified
   * real TradeWars and Major BBS prompts.
   */
  #isCursorAtEnd(): boolean {
    const { x, y } = this.#screen.cursor;
    const lines = this.#screen.display;
    for (let row = lines.length - 1; row >= 0; row -= 1) {
      const line = (lines[row] as string).replace(/\s+$/, "");
      if (line !== "") {
        if (y === row) {
          return x >= line.length - 2;
        }
        return y > row;
      }
    }
    return true;
  }

  /**
   * The current screen state.
   *
   * The body is cached until the next `process`, but `captured_at` is always
   * stamped fresh, so a caller can tell a re-read from a re-render.
   */
  getSnapshot(): Snapshot {
    if (this.#cached === undefined || this.#dirty) {
      const screenText = this.#screen.display.join("\n");
      this.#cached = {
        screen: screenText,
        screen_hash: createHash("sha256").update(screenText, "utf-8").digest("hex"),
        cursor: { x: this.#screen.cursor.x, y: this.#screen.cursor.y },
        cols: this.cols,
        rows: this.rows,
        term: this.term,
        cursor_at_end: this.#isCursorAtEnd(),
        // A screen ending in a space or a colon is a prompt awaiting input.
        // The two strips are deliberately asymmetric, as in the reference:
        // the first removes all trailing whitespace, the second removes only
        // spaces and colons and so stops at a newline. A blank screen
        // therefore reports true, because the row separators survive the
        // second strip but not the first.
        has_trailing_space: trimEnd(screenText, " \t\n\r\v\f") !== trimEnd(screenText, " :"),
        raw_tail: this.#rawTail,
      };
      this.#dirty = false;
    }
    return { ...this.#cached, cursor: { ...this.#cached.cursor }, captured_at: Date.now() / 1000 };
  }

  /**
   * The current screen as one string with ANSI SGR codes.
   *
   * Use this where a consumer needs the *visual* state including colour;
   * a snapshot's plain `screen` field discards style attributes.
   */
  ansiScreen(): string {
    return renderCellRows((y, x) => this.#screen.peek(y, x), this.cols, this.rows).join("\n");
  }

  /** Reset the terminal to its initial state. */
  reset(): void {
    this.#screen.reset();
    this.#dirty = true;
  }

  /** Resize the terminal. */
  resize(cols: number, rows: number): void {
    this.cols = cols;
    this.rows = rows;
    // The screen takes (rows, cols), the reverse of the constructor.
    this.#screen.resize(rows, cols);
    this.#dirty = true;
  }
}

/** Strip any trailing character in `chars` from the end of `value`. */
function trimEnd(value: string, chars: string): string {
  let end = value.length;
  while (end > 0 && chars.includes(value[end - 1] as string)) {
    end -= 1;
  }
  return value.slice(0, end);
}
