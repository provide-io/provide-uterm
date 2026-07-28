//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { compilePySearch, pyEncodeReplace, pyReEscape } from "./regex.ts";

interface EscapeGolden {
  escape: Array<{ name: string; value: string; escaped: string }>;
  encode: Array<{ name: string; value: string; bytes: number[] }>;
  surrogates: Array<{ name: string; code_points: number[]; bytes: number[] }>;
}

const golden = loadGolden<EscapeGolden>("pyescape_golden.json");

describe("escaping a literal for use in a pattern", () => {
  it.each(golden.escape)("$name", (record) => {
    expect(pyReEscape(record.value)).toBe(record.escaped);
  });

  it("escapes a space, which most helpers do not", () => {
    // The escaped text is what the detector reports in its diagnostics, so a
    // port escaping differently shows a different rule to whoever is
    // debugging — even where the two match the same screens.
    expect(pyReEscape("STARDOCK is")).toBe("STARDOCK\\ is");
  });

  it("leaves the characters CPython leaves alone", () => {
    // Colons, slashes, commas and underscores are not special and are not
    // escaped; over-escaping would be as wrong as under-escaping.
    for (const name of ["a colon", "a slash", "a comma", "an underscore", "digits", "plain letters"]) {
      const record = golden.escape.find((entry) => entry.name === name);
      expect(record?.escaped).toBe(record?.value);
    }
  });

  it("produces something that matches the original", () => {
    // The whole point: an escaped literal is a pattern for itself.
    for (const record of golden.escape) {
      expect(compilePySearch(record.escaped).test(record.value)).toBe(true);
    }
  });

  it("stops a literal from behaving as a pattern", () => {
    // Unescaped, "[TL=" is an unterminated class; "(Y/N)" is a group that
    // matches a different string entirely.
    expect(compilePySearch(pyReEscape("[TL=")).test("[TL=")).toBe(true);
    expect(compilePySearch(pyReEscape("(Y/N)")).test("Y")).toBe(false);
    expect(compilePySearch(pyReEscape("(Y/N)")).test("(Y/N)")).toBe(true);
  });
});

describe("encoding text that cannot be encoded", () => {
  it.each(golden.encode)("$name", (record) => {
    expect([...pyEncodeReplace(record.value)]).toStrictEqual(record.bytes);
  });

  it.each(golden.surrogates)("$name", (record) => {
    const value = record.code_points.map((point) => String.fromCharCode(point)).join("");
    expect([...pyEncodeReplace(value)]).toStrictEqual(record.bytes);
  });

  it("substitutes a question mark, not U+FFFD", () => {
    // Encoding replaces with an ASCII question mark where decoding replaces
    // with U+FFFD. The prompt fingerprint hashes these bytes, so reaching for
    // the wrong one diverges every cache key for a screen with a broken
    // character in it.
    expect([...pyEncodeReplace(String.fromCharCode(0xd800))]).toStrictEqual([0x3f]);
    expect([...pyEncodeReplace(String.fromCharCode(0xd800))]).not.toStrictEqual([0xef, 0xbf, 0xbd]);
  });

  it("only replaces the units that are actually broken", () => {
    // A character above the surrogate range is a character. A range check
    // with no upper bound would replace every one of them.
    expect([...pyEncodeReplace("\ue000")]).toStrictEqual([...Buffer.from("\ue000", "utf-8")]);
    expect([...pyEncodeReplace("\ufdfd")]).toStrictEqual([...Buffer.from("\ufdfd", "utf-8")]);
    expect([...pyEncodeReplace("\uffff")]).toStrictEqual([...Buffer.from("\uffff", "utf-8")]);
  });

  it("does not read two low halves as a pair", () => {
    // Pair detection that only checked the second unit would emit these as
    // though they were a character, producing bytes nothing can decode.
    expect([...pyEncodeReplace(String.fromCharCode(0xdc00, 0xdc00))]).toStrictEqual([0x3f, 0x3f]);
    expect([...pyEncodeReplace(String.fromCharCode(0xdc00, 0xd800))]).toStrictEqual([0x3f, 0x3f]);
  });

  it("leaves encodable text exactly as UTF-8", () => {
    expect([...pyEncodeReplace("héllo")]).toStrictEqual([...Buffer.from("héllo", "utf-8")]);
    expect([...pyEncodeReplace("😀")]).toStrictEqual([...Buffer.from("😀", "utf-8")]);
  });
});
