//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Keystroke unescaping and sanitization shared by direct sessions and MCP.
 *
 * Port of the Python module `provide.uterm.sanitizer` and the Go package
 * `sanitizer`.
 */

/** Single-character escapes recognised by {@link unescapeKeys}. */
const SIMPLE_ESCAPES: Readonly<Record<string, string>> = {
  n: "\n",
  r: "\r",
  t: "\t",
  e: "\x1b",
  "0": "\x00",
  "\\": "\\",
  "'": "'",
  '"': '"',
};

/**
 * Build the escape-sequence pattern.
 *
 * Mirrors the Python `re.DOTALL` pattern: the trailing catch-all group must
 * also match a newline, which `[\s\S]` guarantees without relying on flag
 * behaviour. The `u` flag makes the catch-all consume a whole code point
 * rather than half a surrogate pair, matching CPython's code-point scan.
 *
 * Returned fresh per call because a `g`-flagged RegExp carries mutable
 * `lastIndex` state that would leak between scans.
 */
function escapePattern(): RegExp {
  return /\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|([\s\S]))/gu;
}

/**
 * The characters `sanitizeKeystrokes` lets through: printable ASCII plus the
 * terminal input controls.
 *
 * Python spells this `set(string.printable) | {"\r", "\n", "\t", "\x03",
 * "\x1b"}`. `string.printable` is ASCII `0x20`-`0x7e` plus the whitespace
 * run `\t\n\r\x0b\x0c`, so the union is the ASCII printable range plus
 * `\t\n\v\f\r`, `\x03` (Ctrl-C) and `\x1b` (ESC).
 */
const ALLOWED_CONTROLS = new Set(["\t", "\n", "\v", "\f", "\r", "\x03", "\x1b"]);

/** Report whether `char` survives keystroke sanitization. */
function isAllowed(char: string): boolean {
  const code = char.codePointAt(0) as number;
  return (code >= 0x20 && code <= 0x7e) || ALLOWED_CONTROLS.has(char);
}

/**
 * Translate terminal-relevant escape sequences in `raw`.
 *
 * Recognises `\xHH`, `\uHHHH`, and the single-character escapes `\n`, `\r`,
 * `\t`, `\e`, `\0`, `\\`, `\'` and `\"`. Anything else — an unknown escape,
 * a malformed hex run, or a trailing lone backslash — is returned verbatim.
 */
export function unescapeKeys(raw: string): string {
  return raw.replace(escapePattern(), (match, hex2?: string, hex4?: string, char?: string) => {
    if (hex2 !== undefined) {
      return String.fromCodePoint(Number.parseInt(hex2, 16));
    }
    if (hex4 !== undefined) {
      return String.fromCodePoint(Number.parseInt(hex4, 16));
    }
    // The alternation guarantees the catch-all group matched whenever the
    // two hex groups did not, so `char` is always a string here.
    const simple = SIMPLE_ESCAPES[char as string];
    return simple !== undefined ? simple : match;
  });
}

/**
 * Filter non-printable characters while preserving terminal input controls,
 * then cap the result at `maxBytes` UTF-8 bytes.
 *
 * Every surviving character is ASCII, so one character is exactly one UTF-8
 * byte and the cap can be applied by character count. That invariant is what
 * makes this equivalent to Python's encode / slice / `decode(..., "ignore")`
 * round trip, which can never see a split multi-byte sequence here.
 */
export function sanitizeKeystrokes(keys: string, maxBytes = 4096): string {
  let filtered = "";
  for (const char of keys) {
    if (isAllowed(char)) {
      filtered += char;
    }
  }
  return filtered.length <= maxBytes ? filtered : filtered.slice(0, maxBytes);
}

/** Unescape then sanitize keystrokes. */
export function prepareKeystrokes(raw: string, maxBytes = 4096): string {
  return sanitizeKeystrokes(unescapeKeys(raw), maxBytes);
}
