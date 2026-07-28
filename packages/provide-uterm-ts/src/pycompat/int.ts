//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython's `int()` on the ECMAScript engine.
 *
 * Not `Number()` and not `parseInt()`. `int()` accepts underscore separators
 * and Unicode *decimal* digits, refuses anything float-shaped, and raises
 * rather than returning a sentinel — three differences that all show up on
 * values arriving off a wire.
 */

/** Unicode whitespace CPython strips before parsing. */
const WHITESPACE = /^\s*|\s*$/gu;

/**
 * An integer literal as `int()` accepts it.
 *
 * A sign, then digits, with single underscores allowed only *between* digits
 * — leading, trailing and doubled ones are refused, matching the literal
 * syntax CPython reuses here.
 *
 * `\p{Nd}` rather than `[0-9]`: `int("٣")` is 3. Deliberately narrower than
 * `str.isdigit`, which also accepts superscripts that `int()` refuses — the
 * pair that catches a port reusing one predicate for both.
 */
const INTEGER = /^[+-]?\p{Nd}+(?:_\p{Nd}+)*$/u;

/** A single Unicode decimal digit. */
const ONE_DIGIT = /^\p{Nd}$/u;

/**
 * The value of one Unicode decimal digit.
 *
 * Decimal digits come in contiguous runs of ten, so the value is the
 * distance back to the run's zero — found by walking back until the previous
 * code point is no longer a decimal digit.
 */
function decimalValue(char: string): number {
  const cp = char.codePointAt(0) as number;
  // A run is exactly ten long, so nine steps back without hitting the edge
  // means this is the nine.
  for (let offset = 0; offset < 9; offset += 1) {
    if (!ONE_DIGIT.test(String.fromCodePoint(cp - offset - 1))) {
      return offset;
    }
  }
  return 9;
}

/** Rewrite any Unicode decimal digits as ASCII, leaving a sign in place. */
function toAsciiDigits(text: string): string {
  let out = "";
  for (const char of text) {
    out += ONE_DIGIT.test(char) ? String(decimalValue(char)) : char;
  }
  return out;
}

/** Options for {@link safeInt}. */
export interface SafeIntOptions {
  /** Values below this fall back to the default rather than being clamped. */
  minVal?: number;
}

/**
 * CPython's `int()`, or nothing where it would raise.
 *
 * A number truncates toward zero; a boolean is one or zero; a string is
 * parsed as an integer literal. Anything else — including a float-shaped
 * string like `"1.5"`, which a `parseFloat`-based port would silently
 * accept and truncate — has no integer value.
 */
export function pyInt(value: unknown): number | undefined {
  if (typeof value === "boolean") {
    return value ? 1 : 0;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? Math.trunc(value) : undefined;
  }
  if (typeof value !== "string") {
    return undefined;
  }
  const text = value.replace(WHITESPACE, "");
  if (!INTEGER.test(text)) {
    return undefined;
  }
  // Non-ASCII digits are mapped to their numeric value first; nothing else
  // does that for us. The literal shape is already guaranteed by INTEGER, so
  // a plain conversion is enough from here.
  return Number(toAsciiDigits(text.replaceAll("_", "")));
}

/**
 * Coerce to an integer, falling back to a default.
 *
 * A value below `minVal` takes the default rather than being clamped up to
 * it: a caller sending zero columns meant something other than one column,
 * and quietly correcting it would hide the bad input.
 *
 * An absent value is coerced *through* the default, where every other
 * rejection returns it untouched — so a fractional default comes back
 * truncated for a missing value and intact for a malformed one. An asymmetry
 * in the reference rather than a decision here, and pinned as such.
 */
export function safeInt(value: unknown, fallback: number, options: SafeIntOptions = {}): number {
  const parsed = value === null || value === undefined ? pyInt(fallback) : pyInt(value);
  if (parsed === undefined) {
    return fallback;
  }
  if (options.minVal !== undefined && parsed < options.minVal) {
    return fallback;
  }
  return parsed;
}
