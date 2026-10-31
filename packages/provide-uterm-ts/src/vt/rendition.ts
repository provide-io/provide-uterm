//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/** SGR: how a parameter list changes the pen a screen prints with. */

import {
  BG_AIXTERM,
  BG_ANSI,
  type Char,
  defaultChar,
  FG_AIXTERM,
  FG_ANSI,
  FG_BG_256,
  TEXT_ATTRIBUTES,
} from "./char.ts";

/**
 * Apply an SGR parameter list to `pen`'s pending attributes.
 *
 * An empty list is a reset, which is why a bare `ESC [ m` clears
 * everything rather than doing nothing.
 */
export function applyRendition(pen: { attrs: Char }, params: readonly number[]): void {
  const codes = params.length === 0 ? [0] : params;
  for (let i = 0; i < codes.length; i += 1) {
    const code = codes[i] as number;
    if (code === 0) {
      pen.attrs = defaultChar();
      continue;
    }
    const attribute = TEXT_ATTRIBUTES[code];
    if (attribute !== undefined) {
      (pen.attrs[attribute.field] as boolean) = attribute.value;
      continue;
    }
    const fg = FG_ANSI[code] ?? FG_AIXTERM[code];
    if (fg !== undefined) {
      pen.attrs.fg = fg;
      continue;
    }
    const bg = BG_ANSI[code] ?? BG_AIXTERM[code];
    if (bg !== undefined) {
      pen.attrs.bg = bg;
      continue;
    }
    if (code === 38 || code === 48) {
      i = applyExtendedColor(pen, codes, i, code === 38 ? "fg" : "bg");
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
function applyExtendedColor(pen: { attrs: Char }, codes: readonly number[], index: number, field: "fg" | "bg"): number {
  const mode = codes[index + 1];
  if (mode === 5) {
    // Indexed form. A run with no index left consumes the rest, so a
    // trailing parameter is not re-read as an unrelated attribute.
    if (index + 2 >= codes.length) {
      return codes.length - 1;
    }
    const entry = FG_BG_256[codes[index + 2] as number];
    if (entry !== undefined) {
      pen.attrs[field] = entry;
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
    pen.attrs[field] = hex;
    return index + 4;
  }
  // An unknown mode consumes only itself, so the parameters after it are
  // still read as ordinary attributes.
  return index + 1;
}
