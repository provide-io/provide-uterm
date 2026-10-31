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
import { renderCellRows, screenStateSuffix } from "../render/index.ts";
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
   * BBS compatibility. Only CP437 and UTF-8 are recognised, by their common
   * names (see {@link sessionCodec}); any other throws.
   */
  receiveEncoding?: string;
}

/**
 * The codec a receive encoding names: `utf-8` for `utf-8`, `utf8`, `utf_8`
 * or `u8`, `cp437` for `cp437`, `ibm437` or `437`, in any case. They are the
 * two codecs the reference's sessions decode with, by their common names;
 * Python's codec registry knows more aliases for each (`utf`, `cp65001`,
 * `cspc8codepage437`, ...), which this does not. Throws a RangeError for any
 * other name, as Python's codec lookup raises for one it does not know,
 * rather than decoding with a codec the session did not ask for.
 */
export function sessionCodec(encoding: string): "utf-8" | "cp437" {
  const name = encoding.toLowerCase().replaceAll("_", "-");
  if (["utf-8", "utf8", "u8"].includes(name)) {
    return "utf-8";
  }
  if (["cp437", "ibm437", "437"].includes(name)) {
    return "cp437";
  }
  throw new RangeError(`unsupported receive encoding: ${encoding}`);
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
  /**
   * The UTF-8 decoder for the whole stream, or `undefined` in a CP437
   * session. A transport hands over whatever its last read returned, which
   * can end inside a multibyte character: decoding each read on its own
   * turned both halves into U+FFFD. Streaming holds the first until the rest
   * arrives, and genuinely invalid bytes still become one U+FFFD per maximal
   * subpart, as in the reference. A byte order mark is text, as it is to
   * Python's UTF-8 codec, not something to strip.
   */
  readonly #utf8: TextDecoder | undefined;
  #dirty = true;
  #cached: Omit<Snapshot, "captured_at"> | undefined;
  #rawTail = "";

  constructor(options: TerminalEmulatorOptions = {}) {
    this.cols = options.cols ?? 80;
    this.rows = options.rows ?? 25;
    this.term = options.term ?? "ANSI";
    this.receiveEncoding = options.receiveEncoding ?? "cp437";
    const utf8 = sessionCodec(this.receiveEncoding) === "utf-8";
    this.#utf8 = utf8 ? new TextDecoder("utf-8", { ignoreBOM: true }) : undefined;
    this.#screen = new Screen(this.cols, this.rows);
    // DEC special graphics (NetHack's DECgraphics walls, curses line
    // drawing) only in a UTF-8 session, as the Python emulator does: a CP437
    // (BBS) session keeps the stream as it was, so a stray SO in ANSI art
    // never starts drawing lines.
    this.#stream = new Stream(this.#screen, { decGraphics: utf8 });
  }

  /** Decode and feed raw terminal bytes through the emulator. */
  process(data: Uint8Array): void {
    const text = this.#utf8 === undefined ? decodeCp437(data) : this.#utf8.decode(data, { stream: true });
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
   *
   * It is a snapshot a viewer resumes the live stream from, so the rows are
   * followed by the state that stream assumes and the cells do not show: the
   * scroll region, the real cursor (deferred wrap included), insert mode, the
   * character sets, the pen and cursor visibility. See
   * `render/terminal-state.ts`.
   */
  ansiScreen(): string {
    const rows = renderCellRows((y, x) => this.#screen.peek(y, x), this.cols, this.rows).join("\n");
    // Designations and SO/SI are state only where the stream honours them,
    // which is exactly where it draws DEC graphics.
    return rows + screenStateSuffix(this.#screen, { charsets: this.#utf8 !== undefined });
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
