//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Query parameters refused as the reference refuses them.
 *
 * Every expected value comes from `query_params_golden.json`, which
 * `gen_query_params_golden.py` records by putting each raw string through
 * pydantic with the same constraints the reference's routes declare.
 */

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { checkInt, checkString, lastQueryValue, parsePyInt, validationFailure } from "./query-params.ts";

interface Case {
  input: string;
  value?: number | string;
  error?: { type: string; msg: string; input: string; ctx?: Record<string, number> };
}

interface QueryGolden {
  int_1_500: Case[];
  int_ge_0: Case[];
  str_max_100: Case[];
}

const golden = loadGolden<QueryGolden>("query_params_golden.json");

/** What a check should return for one golden case, for a parameter called `name`. */
function expected(name: string, one: Case) {
  if (one.error === undefined) {
    return { ok: true, value: one.value };
  }
  return { ok: false, error: { ...one.error, loc: ["query", name] } };
}

describe("an integer between 1 and 500", () => {
  it.each(golden.int_1_500)("reads %j as pydantic does", (one) => {
    expect(checkInt("limit", one.input, { ge: 1, le: 500 })).toStrictEqual(expected("limit", one));
  });
});

describe("an integer of at least 0", () => {
  it.each(golden.int_ge_0)("reads %j as pydantic does", (one) => {
    expect(checkInt("offset", one.input, { ge: 0 })).toStrictEqual(expected("offset", one));
  });
});

describe("a string of at most 100 characters", () => {
  it.each(golden.str_max_100)("reads a %#th string as pydantic does", (one) => {
    expect(checkString("event", one.input, 100)).toStrictEqual(expected("event", one));
  });
});

describe("parsing an integer", () => {
  it("keeps a value too large for a double exact, so its bound is what refuses it", () => {
    expect(parsePyInt("99999999999999999999")).toBe(99_999_999_999_999_999_999n);
  });

  it("keeps the sign of a negative", () => {
    expect(parsePyInt("-12")).toBe(-12n);
  });
});

describe("a repeated parameter", () => {
  it("is read by its last value, as the reference reads it", () => {
    expect(lastQueryValue(new URLSearchParams("limit=1&limit=2"), "limit")).toBe("2");
  });

  it("is absent when it was never given", () => {
    expect(lastQueryValue(new URLSearchParams("other=1"), "limit")).toBeNull();
  });
});

describe("the refusal", () => {
  it("is a 422 listing every failure", async () => {
    const failed = checkInt("limit", "0", { ge: 1 });
    const errors = failed.ok ? [] : [failed.error];
    const response = validationFailure(errors);
    expect(response.status).toBe(422);
    expect(await response.json()).toStrictEqual({ detail: errors });
  });
});
