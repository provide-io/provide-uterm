//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Query-parameter validation that answers the way the reference's does.
 *
 * The reference declares its query parameters with FastAPI's `Query(...)`, so
 * a bad one is refused by pydantic before the handler runs: a 422 whose
 * `detail` lists every failing parameter, each as `{type, loc, msg, input,
 * ctx}`. A client that reads those — to say which parameter was wrong and
 * why — sees the same objects from this port.
 *
 * Only the constraints the ported routes use are here: an integer with `ge`
 * and `le` bounds, and a string with `max_length`. The parsing rules are
 * pydantic's lax string-to-integer rules, recorded off the reference in
 * `serverrecording_golden.json`: surrounding whitespace is ignored, a sign is
 * allowed, digits may be grouped with single underscores, and a fractional
 * part of zeros is accepted.
 */

/** One validation failure, in pydantic's error shape. */
export interface QueryError {
  type: string;
  loc: [string, string];
  msg: string;
  input: string;
  ctx?: Record<string, number>;
}

/** A parameter's value, or the error that refuses it. */
export type Checked<T> = { ok: true; value: T } | { ok: false; error: QueryError };

/**
 * The whitespace pydantic's parser trims: Unicode `White_Space`.
 *
 * Not JavaScript's `\s`, which also strips U+FEFF and leaves U+0085.
 */
const TRIMMED =
  /^[\t-\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t-\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;

/** An integer as pydantic reads one from a string: sign, grouped digits, zero fraction. */
const INTEGER = /^([+-]?)(\d(?:_?\d)*)(?:\.0+)?$/;

/**
 * The value a query parameter was given, as the reference reads it.
 *
 * When a parameter is repeated the *last* value is the one validated — the
 * reference answers `?limit=1&limit=2` with two entries.
 */
export function lastQueryValue(parameters: URLSearchParams, name: string): string | null {
  const values = parameters.getAll(name);
  return values.length === 0 ? null : (values[values.length - 1] as string);
}

/**
 * Parse an integer the way pydantic does from a string, or nothing.
 *
 * A `bigint` because pydantic's integers are unbounded: a limit of twenty
 * digits is refused as too large, not as unparseable.
 */
export function parsePyInt(raw: string): bigint | undefined {
  const match = INTEGER.exec(raw.replace(TRIMMED, ""));
  if (match === null) {
    return undefined;
  }
  const magnitude = BigInt((match[2] as string).replaceAll("_", ""));
  return match[1] === "-" ? -magnitude : magnitude;
}

/** Refuse `raw` for parameter `name`. */
function refuse(type: string, name: string, msg: string, raw: string, ctx?: Record<string, number>): Checked<never> {
  const error: QueryError = { type, loc: ["query", name], msg, input: raw };
  if (ctx !== undefined) {
    error.ctx = ctx;
  }
  return { ok: false, error };
}

/** An integer parameter with an inclusive lower bound, and an upper one if given. */
export function checkInt(name: string, raw: string, bounds: { ge: number; le?: number }): Checked<number> {
  const value = parsePyInt(raw);
  if (value === undefined) {
    return refuse("int_parsing", name, "Input should be a valid integer, unable to parse string as an integer", raw);
  }
  if (value < BigInt(bounds.ge)) {
    return refuse("greater_than_equal", name, `Input should be greater than or equal to ${bounds.ge}`, raw, {
      ge: bounds.ge,
    });
  }
  if (bounds.le !== undefined && value > BigInt(bounds.le)) {
    return refuse("less_than_equal", name, `Input should be less than or equal to ${bounds.le}`, raw, {
      le: bounds.le,
    });
  }
  return { ok: true, value: Number(value) };
}

/**
 * A string parameter with a maximum length.
 *
 * Counted in code points, as Python counts a `str`: an emoji is one character
 * there and two UTF-16 units here.
 */
export function checkString(name: string, raw: string, maxLength: number): Checked<string> {
  if ([...raw].length > maxLength) {
    return refuse("string_too_long", name, `String should have at most ${maxLength} characters`, raw, {
      max_length: maxLength,
    });
  }
  return { ok: true, value: raw };
}

/** The refusal for every failing parameter, as the reference's framework answers. */
export function validationFailure(errors: readonly QueryError[]): Response {
  return Response.json({ detail: errors }, { status: 422 });
}
