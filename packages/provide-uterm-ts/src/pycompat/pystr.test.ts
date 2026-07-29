//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { pyRepr, pyReprValue, pyStr } from "./index.ts";

interface StrGolden {
  values: Array<{ name: string; value: unknown; text: string; int_text?: string }>;
  reprs: Array<{ name: string; text: string; repr: string }>;
  special: Record<string, string>;
}

const golden = loadGolden<StrGolden>("pystrvalue_golden.json");

/** The corpus's stand-in for a value JSON cannot carry. */
function valueOf(record: { value: unknown }): unknown {
  const named: Record<string, number> = {
    "<nan>": Number.NaN,
    "<inf>": Number.POSITIVE_INFINITY,
    "<-inf>": Number.NEGATIVE_INFINITY,
  };
  return typeof record.value === "string" && record.value in named ? named[record.value] : record.value;
}

describe("a value as the reference writes it", () => {
  it.each(golden.values)("$name", (record) => {
    const value = valueOf(record);
    // A whole number is the one thing that cannot come back exactly: this
    // runtime has a single numeric type, so `1.0` and `1` are one value.
    // Where the reference has an int spelling for the same value, that is
    // the one this must produce — recorded rather than derived, so what it
    // is held to is still the reference's own arithmetic.
    expect(pyStr(value)).toBe(record.int_text ?? record.text);
  });

  it("names nothing the way the reference names it", () => {
    // The case this was written for: a screen that is nothing renders as four
    // characters a caller's pattern can fire on, and `String(null)` would
    // have given a different four.
    expect(pyStr(null)).toBe("None");
    expect(pyStr(undefined)).toBe("None");
    expect(String(null)).not.toBe(pyStr(null));
  });

  it("capitalises the truth values", () => {
    expect(pyStr(true)).toBe("True");
    expect(pyStr(false)).toBe("False");
  });

  it("leaves a word as it was", () => {
    // At the top level a string is itself — the quotes are a `repr` thing.
    expect(pyStr("hello")).toBe("hello");
    expect(pyStr("")).toBe("");
    expect(pyStr("it's")).toBe("it's");
  });

  it("writes the numbers that are not numbers as words", () => {
    expect(pyStr(Number.NaN)).toBe(golden.special.nan);
    expect(pyStr(Number.POSITIVE_INFINITY)).toBe(golden.special.inf);
    expect(pyStr(Number.NEGATIVE_INFINITY)).toBe(golden.special["-inf"]);
  });

  it("keeps a negative zero, which can only ever have been a float", () => {
    // There is no `-0` int in the reference, so this one integral value is
    // not ambiguous and is spelled exactly.
    expect(pyStr(-0)).toBe(golden.special["-0.0"]);
    expect(pyStr(0)).toBe("0");
  });

  it("writes a whole number in full rather than in exponent form", () => {
    expect(pyStr(1e21)).toBe("1000000000000000000000");
    expect(pyStr(2 ** 53)).toBe("9007199254740992");
  });

  it("writes a fraction as the reference writes it", () => {
    expect(pyStr(0.5)).toBe("0.5");
    expect(pyStr(1 / 3)).toBe("0.3333333333333333");
    expect(pyStr(1e-7)).toBe("1e-07");
  });

  it("quotes what is inside a container but not what is outside one", () => {
    expect(pyStr(["a", "b"])).toBe("['a', 'b']");
    expect(pyStr("a")).toBe("a");
    expect(pyReprValue("a")).toBe("'a'");
    expect(pyReprValue(1)).toBe("1");
    expect(pyReprValue(null)).toBe("None");
  });

  it("writes a mapping with its keys quoted", () => {
    expect(pyStr({ a: 1 })).toBe("{'a': 1}");
    expect(pyStr({ a: "b" })).toBe("{'a': 'b'}");
    expect(pyStr({})).toBe("{}");
  });

  it("goes as deep as the value does", () => {
    expect(pyStr([[1], [2]])).toBe("[[1], [2]]");
    expect(pyStr([{ a: 1 }])).toBe("[{'a': 1}]");
    expect(pyStr({ a: [1, null] })).toBe("{'a': [1, None]}");
  });

  it("falls back to the runtime for a value the reference could not hold", () => {
    // A symbol or a function cannot arrive from JSON; rendering it as
    // something is still better than throwing inside a filter.
    expect(pyStr(Symbol("x"))).toBe("Symbol(x)");
    expect(typeof pyStr(() => 1)).toBe("string");
  });
});

describe("a string as the reference writes it down", () => {
  it.each(golden.reprs)("$name", (record) => {
    expect(pyRepr(record.text)).toBe(record.repr);
  });

  it("escapes a control character rather than printing it", () => {
    // These strings are attacker-chosen and end up in logs and refusals, so
    // the difference is between a refusal an operator can read and one that
    // moves their cursor.
    expect(pyRepr("\x1b[31m")).toBe("'\\x1b[31m'");
    expect(pyRepr("\x00")).toBe("'\\x00'");
    expect(pyRepr("\x7f")).toBe("'\\x7f'");
  });

  it("pads a one-digit escape to two", () => {
    // `\x1` is not an escape a reader can trust the length of.
    expect(pyRepr("\x01")).toBe("'\\x01'");
    expect(pyRepr("\x0f")).toBe("'\\x0f'");
  });

  it("names the short escapes rather than numbering them", () => {
    expect(pyRepr("\n")).toBe("'\\n'");
    expect(pyRepr("\r")).toBe("'\\r'");
    expect(pyRepr("\t")).toBe("'\\t'");
    expect(pyRepr("\\")).toBe("'\\\\'");
  });

  it("leaves a space and everything printable alone", () => {
    // The boundary is below space, not at it.
    expect(pyRepr(" ")).toBe("' '");
    expect(pyRepr("~")).toBe("'~'");
    expect(pyRepr("café")).toBe("'café'");
  });

  it("switches quote style rather than escaping an apostrophe", () => {
    expect(pyRepr("it's")).toBe('"it\'s"');
    expect(pyRepr('say "hi"')).toBe("'say \"hi\"'");
    // With both kinds present there is nowhere to switch to, so it escapes.
    expect(pyRepr('it\'s "fine"')).toBe("'it\\'s \"fine\"'");
  });
});
