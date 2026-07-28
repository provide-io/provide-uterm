//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Generic line editor for terminal input with readline-style shortcuts.
 *
 * Port of the Python module `provide.uterm.line_editor` and the Go package
 * `lineeditor`.
 */

/** Carriage return and newline both complete the line. */
const ENTER = "\r";
const NEWLINE = "\n";
/** Both DEL and BS delete the character before the cursor. */
const BACKSPACE = "\x7f";
const BACKSPACE_ALT = "\x08";
/** Move to the start of the line. */
const CTRL_A = "\x01";
/** Move left one character. */
const CTRL_B = "\x02";
/** Move to the end of the line. */
const CTRL_E = "\x05";
/** Move right one character. */
const CTRL_F = "\x06";
/** Kill forward to the end of the line. */
const CTRL_K = "\x0b";
/** Kill backward to the start of the line. */
const CTRL_U = "\x15";
/** Kill the word before the cursor. */
const CTRL_W = "\x17";
/** Erase from the cursor to the end of the line. */
const ERASE_TO_EOL = "\x1b[K";
/** Rung when input would exceed the configured length. */
const BELL = "\x07";

/** Construction options for {@link LineEditor}. */
export interface LineEditorOptions {
  /** Maximum number of characters to accept. Defaults to 80. */
  maxLength?: number;
  /** Mask input with asterisks. Defaults to false. */
  passwordMode?: boolean;
  /**
   * Called for all output, including echoes and cursor movements.
   * Rejections propagate to the caller. Omit for silent mode.
   */
  onWrite?: (data: string) => Promise<unknown>;
}

/**
 * Accumulates input characters until Enter is pressed, with readline-style
 * editing shortcuts and password masking.
 *
 * Features:
 *
 * - Character-by-character buffering until Enter/Return
 * - Full cursor position tracking, enabling mid-line editing
 * - Backspace/Delete, removing the character before the cursor
 * - Ctrl+A (start of line), Ctrl+E (end of line), Ctrl+U (kill backward),
 *   Ctrl+K (kill forward), Ctrl+B (left), Ctrl+F (right),
 *   Ctrl+W (kill word backward)
 * - Password masking, echoing `*` instead of the real character
 * - A configurable maximum line length, which prevents unbounded input
 *
 * Terminal assumptions: a VT100-compatible terminal, which holds for every
 * BBS transport (telnet, SSH, WebSocket). Cursor movement uses relative ANSI
 * sequences, so the editor does not need to know the screen column where
 * input began.
 */
export class LineEditor {
  /** Maximum number of characters to accept. */
  maxLength: number;
  /** Whether input is masked with asterisks. */
  passwordMode: boolean;
  /** The characters entered so far, unmasked. */
  buffer = "";
  /** Index into {@link buffer}; 0 is before the first character. */
  cursorPos = 0;

  readonly #onWrite: ((data: string) => Promise<unknown>) | undefined;

  constructor(options: LineEditorOptions = {}) {
    this.maxLength = options.maxLength ?? 80;
    this.passwordMode = options.passwordMode ?? false;
    this.#onWrite = options.onWrite;
  }

  /** Write to the terminal if an output callback is configured. */
  async #emit(text: string): Promise<void> {
    if (this.#onWrite !== undefined) {
      await this.#onWrite(text);
    }
  }

  /** Return the displayable form of `s`, masked in password mode. */
  #display(s: string): string {
    return this.passwordMode ? "*".repeat(s.length) : s;
  }

  /**
   * Redraw the text from the cursor to the end of the line, erase whatever
   * the removed text left behind, and return the cursor to where it was.
   *
   * Shared by the two kill operations that leave a tail in place.
   */
  #redrawTail(moveLeft: number, remaining: string): string {
    let seq = `\x1b[${moveLeft}D${this.#display(remaining)}${ERASE_TO_EOL}`;
    if (remaining !== "") {
      seq += `\x1b[${remaining.length}D`;
    }
    return seq;
  }

  /** Handle Enter: return the completed line and reset. */
  async #complete(): Promise<string> {
    const result = this.buffer;
    this.buffer = "";
    this.cursorPos = 0;
    await this.#emit("\r\n");
    return result;
  }

  /** Handle Backspace/Delete. */
  async #backspace(): Promise<void> {
    if (this.cursorPos === 0) {
      return;
    }
    const tail = this.buffer.slice(this.cursorPos);
    this.buffer = this.buffer.slice(0, this.cursorPos - 1) + tail;
    this.cursorPos -= 1;
    // Move left one, redraw the tail, overwrite the character the shift left
    // behind, then move back to the cursor.
    await this.#emit(`\x08${this.#display(tail)} \x1b[${tail.length + 1}D`);
  }

  /** Handle Ctrl+W: kill the run of spaces then the word before the cursor. */
  async #killWord(): Promise<void> {
    if (this.cursorPos === 0) {
      return;
    }
    let pos = this.cursorPos;
    while (pos > 0 && this.buffer[pos - 1] === " ") {
      pos -= 1;
    }
    while (pos > 0 && this.buffer[pos - 1] !== " ") {
      pos -= 1;
    }
    const deleted = this.cursorPos - pos;
    const remaining = this.buffer.slice(this.cursorPos);
    this.buffer = this.buffer.slice(0, pos) + remaining;
    await this.#emit(this.#redrawTail(deleted, remaining));
    this.cursorPos = pos;
  }

  /** Handle an ordinary character, inserting it at the cursor. */
  async #insert(ch: string): Promise<void> {
    if (this.buffer.length >= this.maxLength) {
      await this.#emit(BELL);
      return;
    }
    const tail = this.buffer.slice(this.cursorPos);
    this.buffer = this.buffer.slice(0, this.cursorPos) + ch + tail;
    this.cursorPos += 1;
    if (tail === "") {
      await this.#emit(this.passwordMode ? "*" : ch);
      return;
    }
    // Mid-line insert: echo the new character, redraw the tail, move back.
    await this.#emit(`${this.#display(ch + tail)}\x1b[${tail.length}D`);
  }

  /**
   * Process a single character.
   *
   * @returns The completed line if Enter was pressed, otherwise `null`.
   */
  async processChar(ch: string): Promise<string | null> {
    if (ch === ENTER || ch === NEWLINE) {
      return await this.#complete();
    }
    if (ch === BACKSPACE || ch === BACKSPACE_ALT) {
      await this.#backspace();
      return null;
    }
    if (ch === CTRL_A) {
      if (this.cursorPos > 0) {
        await this.#emit(`\x1b[${this.cursorPos}D`);
        this.cursorPos = 0;
      }
      return null;
    }
    if (ch === CTRL_E) {
      const distance = this.buffer.length - this.cursorPos;
      if (distance > 0) {
        await this.#emit(`\x1b[${distance}C`);
        this.cursorPos = this.buffer.length;
      }
      return null;
    }
    if (ch === CTRL_B) {
      if (this.cursorPos > 0) {
        await this.#emit("\x1b[D");
        this.cursorPos -= 1;
      }
      return null;
    }
    if (ch === CTRL_F) {
      if (this.cursorPos < this.buffer.length) {
        await this.#emit("\x1b[C");
        this.cursorPos += 1;
      }
      return null;
    }
    if (ch === CTRL_U) {
      if (this.cursorPos > 0) {
        const remaining = this.buffer.slice(this.cursorPos);
        this.buffer = remaining;
        await this.#emit(this.#redrawTail(this.cursorPos, remaining));
        this.cursorPos = 0;
      }
      return null;
    }
    if (ch === CTRL_K) {
      if (this.cursorPos < this.buffer.length) {
        this.buffer = this.buffer.slice(0, this.cursorPos);
        await this.#emit(ERASE_TO_EOL);
      }
      return null;
    }
    if (ch === CTRL_W) {
      await this.#killWord();
      return null;
    }
    await this.#insert(ch);
    return null;
  }

  /** Reset the buffer and cursor to the empty state. */
  reset(): void {
    this.buffer = "";
    this.cursorPos = 0;
  }

  /** Get the current buffer contents. */
  getBuffer(): string {
    return this.buffer;
  }

  /** Change the maximum line length. */
  setMaxLength(length: number): void {
    this.maxLength = length;
  }

  /** Enable or disable password masking. */
  setPasswordMode(enabled: boolean): void {
    this.passwordMode = enabled;
  }
}
