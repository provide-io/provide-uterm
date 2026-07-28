//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Colour-upgrade helpers: 16-colour SGR and BBS palette tokens to 256-colour
 * or truecolor.
 *
 * These are the upgrade counterparts to `downgradeTo256` / `downgradeTo16` in
 * the `colors` module. Port of the upgrade half of `provide.uterm.ansi`.
 */

import { color256ToRgb, DEFAULT_PALETTE } from "./palette.ts";

/** An SGR escape sequence with a possibly-empty parameter list. */
const SGR_PATTERN = /\x1b\[([0-9;]*)m/g;
/** `{P#}` / `{T#}` legacy BBS palette tokens. */
const TOKEN_PATTERN = /\{([PT])([0-9]{1,3})\}/g;

/** Map an SGR colour code to a base-16 palette index, or `undefined`. */
function mapIndex(code: number): number | undefined {
  if (code >= 30 && code <= 37) {
    return code - 30;
  }
  if (code >= 90 && code <= 97) {
    return 8 + (code - 90);
  }
  if (code >= 40 && code <= 47) {
    return code - 40;
  }
  if (code >= 100 && code <= 107) {
    return 8 + (code - 100);
  }
  return undefined;
}

/** Whether an SGR colour code selects a foreground. */
function isForeground(code: number): boolean {
  return (code >= 30 && code <= 37) || (code >= 90 && code <= 97);
}

/**
 * A bold foreground selects its bright palette entry (0-7 becomes 8-15).
 *
 * Standard DOS/BBS and default xterm (`drawBoldTextInBrightColors`)
 * semantics: `\x1b[1;31m` is bright red, not dim red. Already-bright indices
 * are left alone so bold on a 90-97 code does not run off the range.
 */
function brightenForegroundIndex(index: number): number {
  return index < 8 ? index + 8 : index;
}

/**
 * Walk one SGR sequence, upgrading its 16-colour parameters.
 *
 * The parsing, bold handling and non-colour pass-through are identical for
 * the 256-colour and truecolor upgrades; only the final `38;…` / `48;…`
 * rendering differs, which the caller supplies.
 */
function convertSgr(
  match: string,
  params: string,
  emitFg: (i: number) => string,
  emitBg: (i: number) => string,
): string {
  if (params === "") {
    return match;
  }
  const parts = params.split(";");
  // An explicit 256-colour or truecolor introducer means this sequence has
  // already been upgraded; touching it would corrupt its operands.
  if (parts.includes("38") || parts.includes("48")) {
    return match;
  }
  const bold = parts.includes("1");
  const out: string[] = [];
  for (const part of parts) {
    if (part === "") {
      continue;
    }
    const code = Number.parseInt(part, 10);
    let index = mapIndex(code);
    if (index === undefined) {
      out.push(String(code));
      continue;
    }
    const foreground = isForeground(code);
    if (foreground && bold) {
      index = brightenForegroundIndex(index);
    }
    out.push(foreground ? emitFg(index) : emitBg(index));
  }
  if (out.length === 0) {
    return match;
  }
  return `\x1b[${out.join(";")}m`;
}

/** Walk the `{P#}` / `{T#}` tokens, rendering each palette index. */
function convertTokens(text: string, emit: (kind: string, index: number) => string): string {
  return text.replace(TOKEN_PATTERN, (_match, kind: string, digits: string) =>
    emit(kind, Number.parseInt(digits, 10) % 16),
  );
}

/**
 * Replace SGR 16-colour sequences and `{P#}` / `{T#}` tokens with 256-colour
 * equivalents.
 *
 * @param text ANSI text possibly containing 16-colour SGR codes or BBS
 *   palette tokens.
 * @param palette 16-entry list mapping BBS colour indices to 256-colour
 *   indices. Defaults to {@link DEFAULT_PALETTE}.
 */
export function upgradeTo256(text: string, palette: number[] = DEFAULT_PALETTE): string {
  const entry = (index: number): number => palette[index] as number;
  const converted = convertTokens(text, (kind, index) => `{${kind === "P" ? "F" : "B"}${entry(index)}}`);
  return converted.replace(SGR_PATTERN, (match, params: string) =>
    convertSgr(
      match,
      params,
      (index) => `38;5;${entry(index)}`,
      (index) => `48;5;${entry(index)}`,
    ),
  );
}

/**
 * Replace SGR 16-colour sequences and `{P#}` / `{T#}` tokens with 24-bit
 * truecolor.
 *
 * @param text ANSI text possibly containing 16-colour SGR codes or BBS
 *   palette tokens.
 * @param palette 16-entry list mapping BBS colour indices to 256-colour
 *   indices, from which RGB values are derived. Defaults to
 *   {@link DEFAULT_PALETTE}.
 */
export function upgradeToTruecolor(text: string, palette: number[] = DEFAULT_PALETTE): string {
  const rgbPalette = palette.map((index) => color256ToRgb(index));
  const triple = (index: number): string => (rgbPalette[index] as [number, number, number]).join(";");
  const converted = convertTokens(text, (kind, index) =>
    kind === "P" ? `\x1b[38;2;${triple(index)}m` : `\x1b[48;2;${triple(index)}m`,
  );
  return converted.replace(SGR_PATTERN, (match, params: string) =>
    convertSgr(
      match,
      params,
      (index) => `38;2;${triple(index)}`,
      (index) => `48;2;${triple(index)}`,
    ),
  );
}
