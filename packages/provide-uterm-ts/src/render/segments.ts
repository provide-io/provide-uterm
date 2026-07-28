//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Rendering coloured text into structured segments.
 *
 * Port of `provide.uterm.render.segments`.
 *
 * A terminal client interprets ANSI itself; a structured client — a web UI
 * rendering spans rather than a character grid — needs the same colour
 * information as *data*. These segments are parsed back out of the very ANSI
 * the terminal renders, so the two presentations cannot drift.
 *
 * Colours are semantic names rather than RGB, so a client maps them onto its
 * own theme.
 */

import { normalizeColors } from "../ansi/index.ts";

/** The standard SGR foreground codes, in order from 30. */
const SGR_FOREGROUNDS = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"] as const;

/** The closed set of colour names a segment may carry. */
export const SEGMENT_COLOR_NAMES: readonly string[] = SGR_FOREGROUNDS;

/** The first and last base foreground codes. */
const FG_FIRST = 30;
const FG_LAST = 37;

/** Bright foregrounds sit sixty above their base. */
const BRIGHT_OFFSET = 60;
const BRIGHT_FIRST = 90;
const BRIGHT_LAST = 97;

/** One SGR sequence: escape, bracket, parameters, `m`. */
const SGR = /\x1b\[([0-9;]*)m/y;

/** Any other escape — a cursor move, a clear — which carries no text. */
const OTHER_ESCAPE = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b./y;

/** A run of text sharing one foreground colour and bold flag. */
export interface Segment {
  text: string;
  color: string | undefined;
  bold: boolean;
}

/** The running style, folded through each SGR sequence. */
interface Style {
  color: string | undefined;
  bold: boolean;
}

/**
 * Fold one SGR parameter list into the running style.
 *
 * An extended-colour introducer has its operands skipped rather than read:
 * `38;5;196` is one instruction, and a parser that walked into it would read
 * the 196 as another code and paint the text a colour nobody asked for. The
 * colour itself is left as it was — the reference does not translate an
 * extended colour into a semantic name, because there is no name for it.
 */
function applySgr(params: string, style: Style): Style {
  // No parameters at all means a reset, which is what a bare `ESC[m` is.
  const codes = params === "" ? [0] : params.split(";").map((part) => (part === "" ? 0 : Number.parseInt(part, 10)));
  let { color, bold } = style;

  for (let index = 0; index < codes.length; index += 1) {
    const code = codes[index] as number;
    if (code === 0) {
      color = undefined;
      bold = false;
    } else if (code === 1) {
      bold = true;
    } else if (code === 22) {
      bold = false;
    } else if (code === 39) {
      color = undefined;
    } else if (code >= FG_FIRST && code <= FG_LAST) {
      color = SGR_FOREGROUNDS[code - FG_FIRST];
    } else if (code >= BRIGHT_FIRST && code <= BRIGHT_LAST) {
      // A bright colour is the base colour plus bold, so a client with no
      // separate bright palette still tells the two apart.
      color = SGR_FOREGROUNDS[code - BRIGHT_OFFSET - FG_FIRST];
      bold = true;
    } else if (code === 38 || code === 48) {
      const next = codes[index + 1];
      if (next === 5) {
        index += 2;
      } else if (next === 2) {
        index += 4;
      }
    }
  }
  return { color, bold };
}

/**
 * Parse ANSI-coloured text into segments.
 *
 * Adjacent runs of the same style are merged and empty runs dropped: a client
 * rendering one span per segment would otherwise emit a span per escape
 * sequence, and an empty one for every reset that changed nothing.
 *
 * Escapes that are not SGR carry no text and are dropped, as is a lone escape
 * at the end of the input.
 */
export function ansiToSegments(text: string): Segment[] {
  const segments: Segment[] = [];
  let style: Style = { color: undefined, bold: false };
  let buffer = "";

  const flush = (): void => {
    if (buffer === "") {
      return;
    }
    const last = segments[segments.length - 1];
    if (last !== undefined && last.color === style.color && last.bold === style.bold) {
      last.text += buffer;
    } else {
      segments.push({ text: buffer, color: style.color, bold: style.bold });
    }
    buffer = "";
  };

  let index = 0;
  while (index < text.length) {
    if (text[index] !== "\x1b") {
      buffer += text[index];
      index += 1;
      continue;
    }
    SGR.lastIndex = index;
    const sgr = SGR.exec(text);
    if (sgr !== null) {
      flush();
      style = applySgr(sgr[1] as string, style);
      index = SGR.lastIndex;
      continue;
    }
    OTHER_ESCAPE.lastIndex = index;
    const other = OTHER_ESCAPE.exec(text);
    if (other !== null) {
      index = OTHER_ESCAPE.lastIndex;
      continue;
    }
    // A lone escape with nothing after it.
    index += 1;
  }
  flush();
  return segments;
}

/**
 * Render dialect-token text into segments.
 *
 * The colours come from the same ANSI the terminal renders, so they cannot
 * drift from the token dialect.
 */
export function tokensToSegments(text: string): Segment[] {
  return ansiToSegments(normalizeColors(text));
}
