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
type State = "ground" | "escape" | "charset" | "csi" | "osc-code" | "osc" | "osc-escape";

/** Control characters handled directly in the ground state. */
const BEL = "\x07";
const BS = "\x08";
const HT = "\x09";
const LF = "\x0a";
const VT = "\x0b";
const FF = "\x0c";
const CR = "\x0d";
const SO = "\x0e";
const SI = "\x0f";
const ESC = "\x1b";
const NUL = "\x00";
const DEL = "\x7f";
/** The 8-bit introducers of a CSI and an OSC, and the 8-bit string terminator. */
const CSI_C1 = "\x9b";
const ST_C1 = "\x9c";
const OSC_C1 = "\x9d";

/**
 * What ends a run of text: every control the ground state acts on, and the
 * ones it ignores. Anything else is drawn, a control with no handler too.
 */
const SPECIAL: ReadonlySet<string> = new Set([BEL, BS, HT, LF, VT, FF, CR, SO, SI, ESC, NUL, DEL, CSI_C1, OSC_C1]);

/** Construction options for {@link Stream}. */
export interface StreamOptions {
  /**
   * Honour `ESC ( 0` / `ESC ) 0` (and `ESC ( B`, `ESC ( U` for IBM codepage
   * 437 and `ESC ( V`, into either slot) and SO/SI, so DEC special graphics
   * draw as line-drawing characters, as pyte does with `use_utf8` off and as
   * xterm does. Off by default, which keeps the reference's UTF-8 mode: the
   * designation is consumed but not interpreted, and SO/SI are ignored. A
   * UTF-8 emulator turns it on; a CP437 one leaves it off, so a stray SO in
   * BBS art never starts drawing lines.
   */
  decGraphics?: boolean;
}

/** Feeds terminal output into a {@link Screen}. */
export class Stream {
  readonly #screen: Screen;
  readonly #decGraphics: boolean;
  #state: State = "ground";
  /** Parameter and intermediate bytes accumulated for the current sequence. */
  #buffer = "";
  /** The slot an `ESC (` or `ESC )` designates into, awaiting its code. */
  #charsetSlot: "(" | ")" = "(";
  /**
   * Whether the current run of text met a character with no width. The
   * reference draws each run of text in one call and stops at such a
   * character, so the rest of the run is dropped; a run ends at any control,
   * any sequence, and the end of a `feed`.
   */
  #runStopped = false;

  constructor(screen: Screen, options: StreamOptions = {}) {
    this.#screen = screen;
    this.#decGraphics = options.decGraphics ?? false;
  }

  /** Feed a chunk of terminal output, resuming any sequence in progress. */
  feed(data: string): void {
    this.#runStopped = false;
    for (const char of data) {
      this.#consume(char);
    }
  }

  /** Route one character according to the current state. */
  #consume(char: string): void {
    if (this.#state !== "ground" || SPECIAL.has(char)) {
      this.#runStopped = false;
    }
    if (this.#state === "escape") {
      this.#consumeEscape(char);
      return;
    }
    if (this.#state === "charset") {
      this.#state = "ground";
      // Off, the code is still consumed, as the reference consumes it: the
      // designation is not interpreted, and the code is not text either.
      if (this.#decGraphics) {
        this.#screen.defineCharset(char, this.#charsetSlot);
      }
      return;
    }
    if (this.#state === "csi") {
      this.#consumeCsi(char);
      return;
    }
    if (this.#state !== "ground") {
      // Every state left is one of the three an OSC string passes through.
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
      case CSI_C1:
        this.#state = "csi";
        this.#buffer = "";
        return;
      case OSC_C1:
        this.#state = "osc-code";
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
      // Off, SO and SI are ignored rather than drawn, as the reference
      // ignores them in UTF-8 mode: they are shifts it chooses not to make,
      // not controls it has no handler for.
      case SO:
        if (this.#decGraphics) {
          this.#screen.shiftOut();
        }
        return;
      case SI:
        if (this.#decGraphics) {
          this.#screen.shiftIn();
        }
        return;
      default:
        break;
    }
    // Text, or a control with no handler, which is part of the run of text it
    // arrived in. Such a control has no width, so the screen does not draw it,
    // and the rest of its run is dropped, as the reference and the Go and C#
    // ports drop it. See the stall_divergences section of the vt corpus.
    if (!this.#runStopped && !this.#screen.draw(char)) {
      this.#runStopped = true;
    }
  }

  /** Handle the character after an ESC. */
  #consumeEscape(char: string): void {
    if (char === "[") {
      this.#state = "csi";
      this.#buffer = "";
      return;
    }
    if (char === "]") {
      this.#state = "osc-code";
      return;
    }
    if (char === "(" || char === ")") {
      this.#state = "charset";
      this.#charsetSlot = char;
      return;
    }
    this.#state = "ground";
    switch (char) {
      case "c":
        // RIS, a full reset. It applies whether or not `decGraphics` is on.
        this.#screen.reset();
        return;
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
        // NEL is a line feed in the reference: it returns to column zero only
        // in new line mode.
        this.#screen.linefeed();
        return;
      case "H":
        this.#screen.setTabStop();
        return;
      case "M":
        this.#screen.reverseIndex();
        return;
      default:
        // Every other two-character escape is consumed without effect.
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

  /**
   * Swallow an OSC string up to its terminator: BEL, ST (`ESC \`) or the
   * 8-bit ST. The first character is the command code, whatever it is; `R`
   * and `P` (the palette) end the sequence there, as the reference has no
   * string for them. An ESC followed by anything but `\` is part of the
   * string, not the start of a sequence.
   */
  #consumeOsc(char: string): void {
    if (this.#state === "osc-code") {
      this.#state = char === "R" || char === "P" ? "ground" : "osc";
      return;
    }
    if (this.#state === "osc-escape") {
      this.#state = char === "\\" ? "ground" : "osc";
      return;
    }
    if (char === BEL || char === ST_C1) {
      this.#state = "ground";
    } else if (char === ESC) {
      this.#state = "osc-escape";
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
      case "E":
        screen.cursorDown1(first);
        return;
      case "F":
        screen.cursorUp1(first);
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
        screen.cursorPosition(params[0], params[1]);
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
      case "g":
        screen.clearTabStop(first);
        return;
      case "r":
        screen.setMargins(params[0], params[1]);
        return;
      default:
        // An unimplemented final byte is consumed without effect.
        return;
    }
  }
}
