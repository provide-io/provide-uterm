//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Built-in BBS colour-token dialect handlers.
 *
 * Self-contained string-to-string converters for the extended-token,
 * tilde-code, TWGS brace-token and pipe-code dialects, plus their lookup
 * tables. Port of the Python module `provide.uterm._ansi_dialects`.
 *
 * Every pattern here spells its digit class `[0-9]` rather than `\d`.
 * CPython reads `\d` as Unicode-aware for `str` subjects, so it would accept
 * Arabic-Indic digits inside a token; ECMAScript and Go's RE2 read it as
 * ASCII-only. Writing the class out makes the ASCII intent explicit instead
 * of leaving it to the engine.
 */

/** `{F###}` / `{B###}` / `{P#}` / `{T#}` extended colour tokens. */
const EXT_TOKEN_PATTERN = /\{([FBPT])([0-9]{1,3})\}/g;

/** Single-character colour tags used by the tilde and brace dialects. */
const PREVIEW_COLOR_MAP: Readonly<Record<string, number>> = {
  k: 30,
  r: 31,
  g: 32,
  y: 33,
  b: 34,
  m: 35,
  c: 36,
  w: 37,
};

/** Tilde code to (polarity, colour) pair. */
const TILDE_MAP: Readonly<Record<string, readonly [string, string]>> = {
  "1": ["+", "g"],
  "2": ["+", "w"],
  "3": ["+", "c"],
  "4": ["+", "r"],
  "5": ["+", "m"],
  "6": ["+", "y"],
  "7": ["-", "w"],
  "0": ["-", "x"],
  r: ["+", "r"],
  R: ["+", "r"],
  g: ["+", "g"],
  G: ["+", "g"],
  y: ["+", "y"],
  Y: ["+", "y"],
  b: ["+", "b"],
  B: ["+", "b"],
  m: ["+", "m"],
  M: ["+", "m"],
  c: ["+", "c"],
  C: ["+", "c"],
  w: ["+", "w"],
  W: ["+", "w"],
  d: ["-", "w"],
  D: ["-", "w"],
  E: ["+", "r"],
};

/** Brace token to the escape sequence it stands for. */
const BRACE_TOKEN_MAP: Readonly<Record<string, string>> = {
  "{+c}": "\x1b[1;36m",
  "{-c}": "\x1b[0;36m",
  "{+r}": "\x1b[1;31m",
  "{-r}": "\x1b[0;31m",
  "{+g}": "\x1b[1;32m",
  "{-g}": "\x1b[0;32m",
  "{+y}": "\x1b[1;33m",
  "{-y}": "\x1b[0;33m",
  "{+b}": "\x1b[1;34m",
  "{-b}": "\x1b[0;34m",
  "{+m}": "\x1b[1;35m",
  "{-m}": "\x1b[0;35m",
  "{+w}": "\x1b[1;37m",
  "{+Bw}": "\x1b[1;37m",
  "{-w}": "\x1b[0;37m",
  "{+k}": "\x1b[1;30m",
  "{-k}": "\x1b[0;30m",
  "{-x}": "\x1b[0m",
  "{NK}": "\x1b[0m",
  "{T}": "\x1b[1m",
  "{t}": "\x1b[0m",
};

/** Render one polarity/colour pair as an escape sequence. */
export function emitColor(polarity: string, colorChar: string): string {
  if (colorChar === "x") {
    return "\x1b[0m";
  }
  const code = PREVIEW_COLOR_MAP[colorChar];
  if (code === undefined) {
    return "";
  }
  return polarity === "+" ? `\x1b[0;1;${code}m` : `\x1b[0;${code}m`;
}

/** Palette-token escapes for indices 0-15, dim below 8 and bright above. */
const EXT_P_LOOKUP = Array.from({ length: 16 }, (_, i) => `\x1b[${i % 16 >= 8 ? 90 + (i % 8) : 30 + (i % 8)}m`);
const EXT_T_LOOKUP = Array.from({ length: 16 }, (_, i) => `\x1b[${i % 16 >= 8 ? 100 + (i % 8) : 40 + (i % 8)}m`);

/** Convert `{F###}` / `{B###}` / `{P#}` / `{T#}` tokens to ANSI escapes. */
export function handleExtendedTokens(text: string): string {
  return text.replace(EXT_TOKEN_PATTERN, (_match, kind: string, digits: string) => {
    const value = Number.parseInt(digits, 10);
    if (kind === "F") {
      return `\x1b[38;5;${value}m`;
    }
    if (kind === "B") {
      return `\x1b[48;5;${value}m`;
    }
    if (kind === "P") {
      return EXT_P_LOOKUP[value % 16] as string;
    }
    return EXT_T_LOOKUP[value % 16] as string;
  });
}

/**
 * `~N` tilde codes. The pattern is not DOTALL on either side, so a tilde
 * immediately before a newline is left alone.
 */
const TILDE_PATTERN = /~([^\n])/gu;

/** Pre-built tilde code to escape lookup. */
const TILDE_LOOKUP: Record<string, string> = {};
for (const [code, pair] of Object.entries(TILDE_MAP)) {
  TILDE_LOOKUP[code] = emitColor(pair[0] as string, pair[1] as string);
}

/** Convert `~N` tilde codes to ANSI escapes. */
export function handleTildeCodes(text: string): string {
  return text.replace(TILDE_PATTERN, (match, code: string) => TILDE_LOOKUP[code] ?? match);
}

/** Four-character TWGS token, matched before the shorter tokens. */
const BRACE_4_PATTERN = /\{[+-]Bw\}/g;
/** Three-character colour tags plus the standalone reset/bold tokens. */
const BRACE_3_PATTERN = /\{[+-][a-zA-Z]\}|\{NK\}|\{T\}|\{t\}/g;

/**
 * Convert `{+c}` / `{-x}` brace tokens to ANSI escapes.
 *
 * Includes the TWGS-specific `{+Bw}` header token, which is matched first so
 * the three-character pattern cannot claim its prefix.
 */
export function handleBraceTokens(text: string): string {
  const withHeader = text.replace(BRACE_4_PATTERN, (match) => BRACE_TOKEN_MAP[match] ?? match);
  return withHeader.replace(BRACE_3_PATTERN, (match) => BRACE_TOKEN_MAP[match] ?? match);
}

/** `|00`-`|23` pipe codes — the most common BBS colour format. */
const PIPE_PATTERN = /\|([0-9]{2})/g;

/** DOS colour order to ANSI SGR codes. */
const DOS_TO_ANSI_FG = [30, 34, 32, 36, 31, 35, 33, 37];
const DOS_TO_ANSI_BG = [40, 44, 42, 46, 41, 45, 43, 47];

/** Pre-built pipe code to escape lookup, covering only 00-23. */
const PIPE_LOOKUP: Record<string, string> = {};
for (let i = 0; i < 24; i += 1) {
  const key = String(i).padStart(2, "0");
  if (i <= 7) {
    PIPE_LOOKUP[key] = `\x1b[${DOS_TO_ANSI_FG[i] as number}m`;
  } else if (i <= 15) {
    PIPE_LOOKUP[key] = `\x1b[${(DOS_TO_ANSI_FG[i - 8] as number) + 60}m`;
  } else {
    PIPE_LOOKUP[key] = `\x1b[${DOS_TO_ANSI_BG[i - 16] as number}m`;
  }
}

/** Convert `|NN` pipe codes to ANSI escapes. */
export function handlePipeCodes(text: string): string {
  return text.replace(PIPE_PATTERN, (match, code: string) => PIPE_LOOKUP[code] ?? match);
}
