//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Compilation of CPython `re` patterns on the ECMAScript engine.
 *
 * Operator-supplied patterns (redaction rules, prompt detectors, filters)
 * are written against CPython's `re`. Patterns are handed to the host engine
 * unchanged — the same contract the Go and C# ports hold — with one
 * exception: CPython's leading inline-flag group has no ECMAScript syntax at
 * all and would be a hard compile error rather than a subtle divergence, so
 * it is translated into RegExp flags.
 *
 * Known dialect boundaries that are deliberately **not** translated, because
 * Go's RE2 has the same ones and the ports agree with each other:
 *
 * - `\d`, `\w` and their negations are Unicode-aware in CPython for `str`
 *   subjects, but ASCII-only in ECMAScript and RE2.
 * - Python-only constructs (`(?P<name>…)`, `(?P=name)`, conditionals) are
 *   left for the host engine to reject.
 */

/** CPython inline flag letters that have a direct ECMAScript equivalent. */
const FLAG_TRANSLATIONS: Readonly<Record<string, string>> = {
  i: "i",
  m: "m",
  s: "s",
};

/** A leading CPython inline-flag group, e.g. `(?im)`. */
const LEADING_INLINE_FLAGS = /^\(\?([a-zA-Z]+)\)/;

/**
 * Compile a CPython `re` pattern into a global-matching `RegExp`.
 *
 * A leading inline-flag group is consumed and translated; anything else is
 * passed through verbatim. The result always carries the `g` flag, because
 * every caller in this port scans for all matches the way `re.sub` and
 * `re.finditer` do.
 *
 * @throws {Error} If a leading inline flag has no ECMAScript equivalent.
 * @throws {SyntaxError} If the host engine rejects the pattern.
 */
export function compilePyPattern(pattern: string): RegExp {
  const flags = new Set<string>(["g"]);
  let source = pattern;
  const leading = LEADING_INLINE_FLAGS.exec(pattern);
  if (leading !== null) {
    for (const letter of leading[1] as string) {
      const translated = FLAG_TRANSLATIONS[letter];
      if (translated === undefined) {
        throw new Error(`unsupported inline regex flag: ${letter}`);
      }
      flags.add(translated);
    }
    source = pattern.slice(leading[0].length);
  }
  return new RegExp(source, [...flags].join(""));
}
