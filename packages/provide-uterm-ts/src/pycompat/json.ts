//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython-exact canonical JSON.
 *
 * `JSON.stringify` is close to `json.dumps` but not close enough where the
 * bytes are signed. It leaves DEL and non-ASCII characters bare, formats
 * exponents with one digit, and has no key-sorting option — and the identity
 * signature is an HMAC over exactly these bytes, so each of those is a
 * signature mismatch rather than a cosmetic difference.
 *
 * ## The one thing this cannot reproduce
 *
 * JavaScript has a single number type, so an integral value carries no trace
 * of whether it began life as a Python `int` or a Python `float`. This
 * renders integral values the way CPython renders an `int` (`1`, not `1.0`),
 * which is right for the realistic claim shapes — strings, ints, booleans —
 * and wrong for a claim holding a whole-valued float. The corpus records the
 * divergence rather than leaving it to be discovered from a failing
 * signature check.
 */

/** Options for {@link pyJsonDumps}. */
export interface PyJsonDumpsOptions {
  /** Sort object keys. Defaults to `true`, matching the canonical payload. */
  sortKeys?: boolean;
  /** Escape non-ASCII as `\uXXXX`. Defaults to `true`, as CPython does. */
  ensureAscii?: boolean;
  /**
   * Item and key separators.
   *
   * Defaults to the compact `[",", ":"]` the canonical payload uses. Pass
   * CPython's own default `[", ", ": "]` when emulating a plain
   * `json.dumps(...)` call, which is what the recording stores measure their
   * byte sizes against.
   */
  separators?: readonly [string, string];
}

/** CPython's short escapes, by code point. */
const SHORT_ESCAPES: Readonly<Record<number, string>> = {
  8: "\\b",
  9: "\\t",
  10: "\\n",
  12: "\\f",
  13: "\\r",
  34: '\\"',
  92: "\\\\",
};

/** Render one code unit as a lowercase `\uXXXX` escape. */
function unicodeEscape(codeUnit: number): string {
  return `\\u${codeUnit.toString(16).padStart(4, "0")}`;
}

/**
 * Encode a string the way `json.encoder.py_encode_basestring{,_ascii}` does.
 *
 * Under `ensureAscii` every code unit above 0x7e is escaped, which for an
 * astral character means the two surrogate halves — exactly what CPython
 * emits. DEL is escaped as well, which `JSON.stringify` does not do.
 */
function encodeString(value: string, ensureAscii: boolean): string {
  let out = '"';
  for (let i = 0; i < value.length; i += 1) {
    const codeUnit = value.charCodeAt(i);
    const short = SHORT_ESCAPES[codeUnit];
    if (short !== undefined) {
      out += short;
    } else if (codeUnit < 0x20) {
      out += unicodeEscape(codeUnit);
    } else if (codeUnit < 0x7f) {
      out += value[i];
    } else if (ensureAscii) {
      out += unicodeEscape(codeUnit);
    } else {
      out += value[i];
    }
  }
  return `${out}"`;
}

/**
 * Render a non-integral float the way CPython's `repr` does.
 *
 * Both runtimes produce the shortest round-tripping digits; they disagree on
 * when to switch to exponent notation and on how to format the exponent.
 * CPython uses fixed notation for exponents in `[-4, 16)` and pads the
 * exponent to two digits.
 */
function floatRepr(value: number): string {
  const [mantissa, exponentText] = value.toExponential().split("e") as [string, string];
  const exponent = Number(exponentText);
  const negative = mantissa.startsWith("-");
  const digits = (negative ? mantissa.slice(1) : mantissa).replace(".", "");
  const sign = negative ? "-" : "";

  if (exponent >= -4 && exponent < 16) {
    if (exponent < 0) {
      return `${sign}0.${"0".repeat(-exponent - 1)}${digits}`;
    }
    // Only non-integral values reach here, so there is always at least one
    // digit left after the decimal point — no padding or ".0" suffix needed.
    return `${sign}${digits.slice(0, exponent + 1)}.${digits.slice(exponent + 1)}`;
  }
  // Exponent notation is reachable here only for exponents below -4. Every
  // double at or above 2**53 — so every value with an exponent of 16 or more —
  // is integral, and integral values take the int path before reaching this
  // function. The exponent sign is therefore always negative.
  const head = digits[0] as string;
  const tail = digits.slice(1);
  const exponentDigits = Math.abs(exponent).toString().padStart(2, "0");
  return `${sign}${head}${tail === "" ? "" : `.${tail}`}e-${exponentDigits}`;
}

/** Render a number, choosing CPython's int or float rules by integrality. */
function encodeNumber(value: number): string {
  if (!Number.isFinite(value)) {
    throw new TypeError(`Object of type float is not JSON serializable: ${value}`);
  }
  if (!Number.isInteger(value)) {
    return floatRepr(value);
  }
  // `String(1e21)` is "1e+21", but CPython renders an int in full.
  return Number.isSafeInteger(value) ? String(value) : BigInt(value).toString();
}

/** Serialise `value` to CPython-canonical JSON. */
export function pyJsonDumps(value: unknown, options: PyJsonDumpsOptions = {}): string {
  const sortKeys = options.sortKeys ?? true;
  const ensureAscii = options.ensureAscii ?? true;
  const [itemSeparator, keySeparator] = options.separators ?? [",", ":"];

  const encode = (node: unknown): string => {
    if (node === null) {
      return "null";
    }
    if (typeof node === "boolean") {
      return node ? "true" : "false";
    }
    if (typeof node === "number") {
      return encodeNumber(node);
    }
    if (typeof node === "string") {
      return encodeString(node, ensureAscii);
    }
    if (Array.isArray(node)) {
      return `[${node.map(encode).join(itemSeparator)}]`;
    }
    if (typeof node === "object") {
      const keys = Object.keys(node as Record<string, unknown>);
      if (sortKeys) {
        keys.sort();
      }
      const body = keys
        .map(
          (key) => `${encodeString(key, ensureAscii)}${keySeparator}${encode((node as Record<string, unknown>)[key])}`,
        )
        .join(itemSeparator);
      return `{${body}}`;
    }
    throw new TypeError(`Object of type ${typeof node} is not JSON serializable`);
  };

  return encode(value);
}
