//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { floatRepr } from "./json.ts";

/**
 * CPython string predicates that JavaScript spells differently.
 *
 * Part of the `pycompat` layer: these exist so a port can ask the question
 * CPython would ask, rather than the nearest JavaScript equivalent.
 */

/**
 * Code-point ranges CPython calls digits that `\p{Nd}` does not cover.
 *
 * `str.isdigit()` accepts any character whose Unicode numeric type is Decimal
 * *or* Digit. `\p{Nd}` is the Decimal half; this is the rest — superscripts,
 * subscripts, circled and parenthesised digits, and a few historic scripts.
 *
 * Derived from CPython's Unicode 15.1.0 database and checked
 * against it in full by the test suite, not sampled.
 */
export const DIGIT_NOT_DECIMAL_RANGES: ReadonlyArray<readonly [number, number]> = [
  [0x00b2, 0x00b3],
  [0x00b9, 0x00b9],
  [0x1369, 0x1371],
  [0x19da, 0x19da],
  [0x2070, 0x2070],
  [0x2074, 0x2079],
  [0x2080, 0x2089],
  [0x2460, 0x2468],
  [0x2474, 0x247c],
  [0x2488, 0x2490],
  [0x24ea, 0x24ea],
  [0x24f5, 0x24fd],
  [0x24ff, 0x24ff],
  [0x2776, 0x277e],
  [0x2780, 0x2788],
  [0x278a, 0x2792],
  [0x10a40, 0x10a43],
  [0x10e60, 0x10e68],
  [0x11052, 0x1105a],
  [0x1f100, 0x1f10a],
];

/**
 * One class covering both halves.
 *
 * Built from the table rather than hand-written so the two cannot drift, and
 * with the `u` flag so it matches by code point — an astral digit is one
 * character here, not two surrogate halves.
 */
const DIGIT = new RegExp(
  `^[\\p{Nd}${DIGIT_NOT_DECIMAL_RANGES.map(([start, end]) =>
    start === end ? `\\u{${start.toString(16)}}` : `\\u{${start.toString(16)}}-\\u{${end.toString(16)}}`,
  ).join("")}]+$`,
  "u",
);

/**
 * CPython's `str.isdigit()`.
 *
 * Not `/^\d+$/`: that is ASCII-only, and the difference is load-bearing where
 * this is used. The regex-safety validator decides whether `{...}` is a
 * counted quantifier by asking this of its body, and a quantifier is what
 * triggers its nested-quantifier rejection — so an ASCII-only check would let
 * `(a+){\u0663}` through a guard that exists to stop a caller pinning the
 * event loop.
 *
 * An empty string is not digits, matching CPython.
 */
export function pyIsDigit(text: string): boolean {
  return text !== "" && DIGIT.test(text);
}

/**
 * A string as Python's `repr()` writes it.
 *
 * Two things a plain quoting would get wrong. Python switches quote style
 * rather than escaping: a string holding an apostrophe and no double quote is
 * written in double quotes. And a control character is escaped rather than
 * printed — the difference between a refusal an operator can read and one that
 * moves their cursor, which matters because these strings are attacker-chosen
 * and end up in logs.
 */
export function pyRepr(text: string): string {
  const escaped = [...text]
    .map((character) => {
      const code = character.codePointAt(0) as number;
      if (character === "\\") {
        return "\\\\";
      }
      if (character === "\n") {
        return "\\n";
      }
      if (character === "\r") {
        return "\\r";
      }
      if (character === "\t") {
        return "\\t";
      }
      // Everything else below space, and the delete character. Printable
      // non-ASCII is left alone, which is what Python 3 does.
      if (code < 0x20 || code === 0x7f) {
        return `\\x${code.toString(16).padStart(2, "0")}`;
      }
      return character;
    })
    .join("");
  if (escaped.includes("'") && !escaped.includes('"')) {
    return `"${escaped}"`;
  }
  return `'${escaped.replaceAll("'", "\\'")}'`;
}

/**
 * A value as CPython's `str()` writes it.
 *
 * `String(x)` agrees on strings and on most whole numbers and disagrees on
 * nearly everything else: `None` is `None` rather than `null`, `True` is
 * `True` rather than `true`, and the infinities are words rather than
 * `Infinity`.
 *
 * That is only cosmetic until something *decides* from the text. It is used
 * where a reference module renders an arbitrary value and then matches a
 * caller's pattern against the result — a screen that is `None` becomes four
 * characters a pattern can fire on, and reaching for the host runtime's
 * `String` there would quietly change which sessions an agent is told about.
 *
 * One thing this cannot recover: JavaScript has a single numeric type, so an
 * integral `float` and an `int` are the same value here and both render
 * without the `.0` CPython gives the float. A negative zero and the
 * non-finite values are floats in CPython whatever else they are, so those
 * are exact.
 */
export function pyStr(value: unknown): string {
  if (value === null || value === undefined) {
    return "None";
  }
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number") {
    return numberStr(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(pyReprValue).join(", ")}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return `{${entries.map(([key, item]) => `${pyRepr(key)}: ${pyReprValue(item)}`).join(", ")}}`;
  }
  return String(value);
}

/**
 * A value as it is written *inside* a container.
 *
 * Which is `repr` rather than `str`, so a word in a list keeps its quotes.
 * Every other kind renders the same either way.
 */
export function pyReprValue(value: unknown): string {
  return typeof value === "string" ? pyRepr(value) : pyStr(value);
}

/** A number as CPython writes one. */
function numberStr(value: number): string {
  if (Number.isNaN(value)) {
    return "nan";
  }
  if (value === Number.POSITIVE_INFINITY) {
    return "inf";
  }
  if (value === Number.NEGATIVE_INFINITY) {
    return "-inf";
  }
  // A negative zero is a float in CPython whatever else it is — there is no
  // `-0` int — so this one integral value can be spelled exactly.
  if (Object.is(value, -0)) {
    return "-0.0";
  }
  // Everything else integral is ambiguous: an `int` and a whole-valued
  // `float` are one value here. The int spelling is chosen, as everywhere
  // else in this package.
  return Number.isInteger(value) ? intStr(value) : floatRepr(value);
}

/** A whole number, in full rather than in exponent form. */
function intStr(value: number): string {
  return Number.isSafeInteger(value) ? String(value) : BigInt(value).toString();
}
