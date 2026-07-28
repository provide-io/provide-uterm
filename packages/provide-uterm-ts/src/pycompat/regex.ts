//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Compilation of CPython `re` patterns on the ECMAScript engine.
 *
 * Operator-supplied patterns (redaction rules, prompt detectors, filters)
 * are written against CPython's `re`. Patterns are handed to the host engine
 * as close to unchanged as they can be — the same contract the Go and C#
 * ports hold — with two exceptions, both cases where leaving the pattern
 * alone would be wrong rather than merely different:
 *
 * - CPython's leading inline-flag group has no ECMAScript syntax at all and
 *   would be a hard compile error, so it becomes RegExp flags.
 * - `\A` and `\Z` anchor to the whole subject in CPython and in Go's RE2. In
 *   ECMAScript they are identity escapes meaning the letters A and Z, so an
 *   operator's rule would quietly match the wrong thing instead of failing.
 *   They become lookarounds that say the same thing.
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
 * ECMAScript spellings of CPython's whole-string anchors.
 *
 * `^` and `$` are per-line under MULTILINE in both dialects; `\A` and `\Z`
 * stay pinned to the ends of the subject however many lines it has. There is
 * no ECMAScript syntax for that, but a lookaround for "no character in that
 * direction" says the same thing.
 */
const ANCHOR_TRANSLATIONS: Readonly<Record<string, string>> = {
  A: "(?<![\\s\\S])",
  Z: "(?![\\s\\S])",
};

/**
 * Rewrite CPython's whole-string anchors into ECMAScript lookarounds.
 *
 * Left alone, `\A` is an identity escape in ECMAScript and means the letter
 * A — so an operator's rule would quietly match the wrong thing rather than
 * failing loudly. Inside a character class CPython refuses the escape
 * outright, and so does this: an anchor is not a class member, and reading it
 * as a literal letter would accept a rule the reference rejects.
 *
 * @throws {SyntaxError} On an anchor escape inside a character class.
 */
function translateAnchors(pattern: string): string {
  let out = "";
  let inClass = false;
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index] as string;
    if (char === "\\") {
      const next = pattern[index + 1];
      const anchor = next === undefined ? undefined : ANCHOR_TRANSLATIONS[next];
      if (anchor !== undefined) {
        if (inClass) {
          throw new SyntaxError(`bad escape \\${next} in character class`);
        }
        out += anchor;
        index += 1;
        continue;
      }
      // Any other escape passes through with whatever it escapes, so a
      // doubled backslash cannot be mistaken for the start of one.
      out += char + (next ?? "");
      index += 1;
      continue;
    }
    if (char === "[") {
      inClass = true;
    } else if (char === "]") {
      inClass = false;
    }
    out += char;
  }
  return out;
}

/**
 * Split a CPython pattern into its ECMAScript source and any leading flags.
 *
 * @throws {Error} If a leading inline flag has no ECMAScript equivalent.
 */
function translate(pattern: string): { source: string; flags: Set<string> } {
  const flags = new Set<string>();
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
  return { source: translateAnchors(source), flags };
}

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
  const { source, flags } = translate(pattern);
  flags.add("g");
  return new RegExp(source, [...flags].join(""));
}

/** Options for {@link compilePySearch}. */
export interface PySearchOptions {
  /** Match without regard to case, as `re.IGNORECASE`. */
  ignoreCase?: boolean;
}

/**
 * Compile a CPython `re` pattern for one-shot `search`, under `re.MULTILINE`.
 *
 * Deliberately **not** global. A global pattern carries `lastIndex` between
 * calls, so the same pattern asked about the same screen twice answers
 * differently the second time — a detector built on one would find every
 * other prompt and miss the rest.
 *
 * Case-sensitive unless asked otherwise, because prompt authors rely on exact
 * case to tell prompts apart. Exclusion patterns pass `ignoreCase`, since
 * those are broad guards rather than precise matches.
 *
 * @throws {Error} If a leading inline flag has no ECMAScript equivalent.
 * @throws {SyntaxError} If the host engine rejects the pattern.
 */
export function compilePySearch(pattern: string, options: PySearchOptions = {}): RegExp {
  const { source, flags } = translate(pattern);
  // Always multiline: a screen is one string of many lines, and every caller
  // of this anchors against those lines.
  flags.add("m");
  if (options.ignoreCase === true) {
    flags.add("i");
  }
  return new RegExp(source, [...flags].join(""));
}

/**
 * The exact set CPython's `re.escape` escapes.
 *
 * Since 3.7 it stopped escaping everything non-alphanumeric and settled on
 * this list — which still includes the whitespace characters, so a space
 * becomes `\ `. Most hand-written escape helpers leave a space alone, and the
 * difference shows up in the detector's diagnostics: the escaped text is what
 * an operator reads back when a rule is not firing.
 */
const RE_ESCAPE_CHARS = new Set("()[]{}?*+-|^$\\.&~# \t\n\r\v\f");

/** Escape a string so it matches itself, as CPython's `re.escape`. */
export function pyReEscape(value: string): string {
  let out = "";
  for (const char of value) {
    out += RE_ESCAPE_CHARS.has(char) ? `\\${char}` : char;
  }
  return out;
}

/** An ASCII question mark — what CPython substitutes when encoding fails. */
const ENCODE_REPLACEMENT = "?";

/** A UTF-16 unit that is half of a pair. */
function isSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdfff;
}

/**
 * Encode as UTF-8 the way `str.encode(errors="replace")` does.
 *
 * The replacement is an ASCII question mark, not the U+FFFD that *decoding*
 * substitutes. The prompt fingerprint hashes these bytes, so reaching for the
 * wrong character diverges every cache key for a screen carrying an unpaired
 * surrogate.
 *
 * A surrogate that is half of a valid pair is a real character and encodes
 * normally; only the unpaired ones are replaced.
 */
export function pyEncodeReplace(value: string): Buffer {
  let out = "";
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (!isSurrogate(code)) {
      out += value[index];
      continue;
    }
    const next = index + 1 < value.length ? value.charCodeAt(index + 1) : Number.NaN;
    const paired = code <= 0xdbff && next >= 0xdc00 && next <= 0xdfff;
    if (paired) {
      out += value.slice(index, index + 2);
      index += 1;
      continue;
    }
    out += ENCODE_REPLACEMENT;
  }
  return Buffer.from(out, "utf-8");
}
