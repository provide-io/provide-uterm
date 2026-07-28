//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The escape-sequence parser.
 *
 * Port of the observable behaviour of `pyte.Stream`. The parser is a resumable
 * state machine: a sequence split across two `feed` calls is reassembled, which
 * matters because a WebSocket frame boundary can land anywhere.
 *
 * An unrecognised sequence is consumed and discarded rather than printed. A
 * terminal that printed the bytes of a sequence it did not implement would
 * corrupt the screen far worse than ignoring it.
 */

import type { Screen } from "./screen.ts";

/**
 * Parser states. A union of literals rather than an enum, because an enum is
 * not erasable syntax and this package compiles under `erasableSyntaxOnly`.
 */
type State = "ground" | "escape" | "csi" | "osc";

/** Control characters handled directly in the ground state. */
const BEL = "\x07";
const BS = "\x08";
const HT = "\x09";
const LF = "\x0a";
const VT = "\x0b";
const FF = "\x0c";
const CR = "\x0d";
const ESC = "\x1b";
const NUL = "\x00";
const DEL = "\x7f";

/** Feeds terminal output into a {@link Screen}. */
export class Stream {
  readonly #screen: Screen;
  #state: State = "ground";
  /** Parameter and intermediate bytes accumulated for the current sequence. */
  #buffer = "";

  constructor(screen: Screen) {
    this.#screen = screen;
  }

  /** Feed a chunk of terminal output, resuming any sequence in progress. */
  feed(data: string): void {
    for (const char of data) {
      this.#consume(char);
    }
  }

  /** Route one character according to the current state. */
  #consume(char: string): void {
    if (this.#state === "escape") {
      this.#consumeEscape(char);
      return;
    }
    if (this.#state === "csi") {
      this.#consumeCsi(char);
      return;
    }
    if (this.#state === "osc") {
      this.#consumeOsc(char);
      return;
    }
    this.#consumeGround(char);
  }

  /** Handle a character outside any escape sequence. */
  #consumeGround(char: string): void {
    switch (char) {
      case ESC:
        this.#state = "escape";
        this.#buffer = "";
        return;
      case CR:
        this.#screen.carriageReturn();
        return;
      case LF:
      case VT:
      case FF:
        this.#screen.linefeed();
        return;
      case BS:
        this.#screen.backspace();
        return;
      case HT:
        this.#screen.tab();
        return;
      case BEL:
        return;
      case NUL:
      case DEL:
        // Explicitly ignored, matching the Go port.
        return;
      default:
        break;
    }
    // Any other control has no handler. The reference stalls its parser here
    // and swallows the rest of the stream, so one stray byte would freeze the
    // display for good; the Go port draws the character instead and this port
    // follows Go, because the ports are meant to agree with each other and a
    // stall is a bug. See the stall_divergences section of the vt corpus.
    this.#screen.draw(char);
  }

  /** Handle the character after an ESC. */
  #consumeEscape(char: string): void {
    if (char === "[") {
      this.#state = "csi";
      this.#buffer = "";
      return;
    }
    if (char === "]") {
      this.#state = "osc";
      this.#buffer = "";
      return;
    }
    this.#state = "ground";
    switch (char) {
      case "7":
        this.#screen.saveCursor();
        return;
      case "8":
        this.#screen.restoreCursor();
        return;
      case "D":
        this.#screen.index();
        return;
      case "E":
        // pyte's NEL moves down without returning to column zero.
        this.#screen.index();
        return;
      case "M":
        this.#screen.reverseIndex();
        return;
      default:
        // Charset selection and every other two-character escape is consumed
        // without effect.
        return;
    }
  }

  /**
   * Accumulate a CSI sequence until its final byte arrives.
   *
   * Only digits, the `;` separator and the private-marker prefixes
   * accumulate. Every other byte ends the sequence — including the
   * intermediates a stricter VT parser would collect, because the reference
   * dispatches on them and then treats the *next* byte as ordinary text.
   * `ESC [ 1 ! b` therefore prints a `b`.
   */
  #consumeCsi(char: string): void {
    if ((char >= "0" && char <= "9") || char === ";" || char === "?" || char === ">" || char === "<" || char === "=") {
      this.#buffer += char;
      return;
    }
    this.#state = "ground";
    this.#dispatchCsi(char);
  }

  /** Swallow an OSC string up to its terminator. */
  #consumeOsc(char: string): void {
    if (char === BEL) {
      this.#state = "ground";
      return;
    }
    // ST arrives as ESC \; the backslash is consumed by the escape state.
    if (char === ESC) {
      this.#state = "escape";
    }
  }

  /** Parse the accumulated parameters and apply the sequence. */
  #dispatchCsi(final: string): void {
    const isPrivate = this.#buffer.startsWith("?");
    const body = isPrivate ? this.#buffer.slice(1) : this.#buffer;
    // An empty parameter is zero, which is what makes `ESC [ m` a reset and
    // `ESC [ H` a home.
    const params = body === "" ? [] : body.split(";").map((part) => (part === "" ? 0 : Number.parseInt(part, 10)));
    const first = params[0] ?? 0;
    const screen = this.#screen;

    switch (final) {
      case "A":
        screen.cursorUp(first);
        return;
      case "B":
      case "e":
        screen.cursorDown(first);
        return;
      case "C":
      case "a":
        screen.cursorForward(first);
        return;
      case "D":
        screen.cursorBack(first);
        return;
      case "H":
      case "f":
        screen.cursorPosition(params[0] ?? 1, params[1] ?? 1);
        return;
      case "G":
      case "`":
        screen.cursorToColumn(first);
        return;
      case "d":
        screen.cursorToLine(first);
        return;
      case "J":
        screen.eraseInDisplay(first);
        return;
      case "K":
        screen.eraseInLine(first);
        return;
      case "X":
        screen.eraseCharacters(first);
        return;
      case "L":
        screen.insertLines(first);
        return;
      case "M":
        screen.deleteLines(first);
        return;
      case "@":
        screen.insertCharacters(first);
        return;
      case "P":
        screen.deleteCharacters(first);
        return;
      case "m":
        screen.selectGraphicRendition(params);
        return;
      case "h":
        for (const mode of params) {
          screen.setMode(mode, isPrivate);
        }
        return;
      case "l":
        for (const mode of params) {
          screen.resetMode(mode, isPrivate);
        }
        return;
      case "r":
        if (params.length < 2) {
          screen.resetMargins();
          return;
        }
        screen.setMargins(params[0] as number, params[1] as number);
        return;
      default:
        // An unimplemented final byte is consumed without effect.
        return;
    }
  }
}
