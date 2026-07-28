//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { compilePySearch } from "./regex.ts";

interface SearchGolden {
  cases: Array<{
    name: string;
    pattern: string;
    subject: string;
    ignore_case: boolean;
    matched: boolean;
    start: number | null;
    text: string | null;
    second_start: number | null;
  }>;
  invalid: Array<{ name: string; pattern: string; error: string }>;
}

const golden = loadGolden<SearchGolden>("pysearch_golden.json");

describe("searching a screen with a Python pattern", () => {
  it.each(golden.cases)("$name", (record) => {
    const compiled = compilePySearch(record.pattern, { ignoreCase: record.ignore_case });
    const match = compiled.exec(record.subject);
    expect(match !== null).toBe(record.matched);
    expect(match?.index ?? null).toBe(record.start);
    expect(match?.[0] ?? null).toBe(record.text);
  });

  it("answers the same way when asked twice", () => {
    // A global pattern carries lastIndex between calls, so a detector built
    // on one would find every other prompt and miss the rest.
    for (const record of golden.cases) {
      const compiled = compilePySearch(record.pattern, { ignoreCase: record.ignore_case });
      compiled.exec(record.subject);
      expect(compiled.exec(record.subject)?.index ?? null).toBe(record.second_start);
      expect(record.second_start).toBe(record.start);
    }
  });

  it("is case-sensitive unless told otherwise", () => {
    // Prompt authors rely on exact case to tell prompts apart, so a positive
    // pattern that stopped caring would fire on the wrong screens.
    expect(compilePySearch("stardock").test("STARDOCK")).toBe(false);
    expect(compilePySearch("stardock", { ignoreCase: true }).test("STARDOCK")).toBe(true);
  });

  it("anchors ^ and $ per line", () => {
    // A screen is one string of many lines; without this only the first and
    // last line could ever be anchored against.
    expect(compilePySearch("^Command").test("first\nCommand:")).toBe(true);
    expect(compilePySearch("closed$").test("it closed\nnext")).toBe(true);
  });

  it("anchors \\A and \\Z to the whole string", () => {
    // ECMAScript has neither and reads \A as the letter A, so an operator's
    // rule would silently match the wrong thing rather than failing.
    expect(compilePySearch("\\ASTARDOCK").test("first\nSTARDOCK")).toBe(false);
    expect(compilePySearch("\\AWelcome").test("Welcome\nmore")).toBe(true);
    expect(compilePySearch("closed\\Z").test("closed\nmore")).toBe(false);
    expect(compilePySearch("closed\\Z").test("it closed")).toBe(true);
  });

  it("leaves an escaped backslash alone", () => {
    // \\A is a literal backslash then an A, not an anchor.
    expect(compilePySearch("\\\\A").test("\\A")).toBe(true);
    expect(compilePySearch("\\\\A").test("A")).toBe(false);
  });

  it("refuses an anchor inside a character class, as CPython does", () => {
    // An anchor is not a class member. A port that quietly read it as a
    // literal letter would accept a rule the reference rejects.
    for (const record of golden.invalid) {
      expect(() => compilePySearch(record.pattern)).toThrow();
    }
  });

  it("knows where a character class ends", () => {
    // An anchor after a class is still an anchor; a scanner that never left
    // the class would refuse the whole pattern.
    expect(compilePySearch("[0-9]+\\Z").test("abc 123")).toBe(true);
    expect(compilePySearch("[0-9]+\\Z").test("123 abc")).toBe(false);
    expect(() => compilePySearch("[0-9]\\A")).not.toThrow();
  });

  it("refuses a pattern that ends mid-escape", () => {
    // CPython calls this "bad escape (end of pattern)"; either way a trailing
    // backslash is not a pattern.
    expect(() => compilePySearch("abc\\")).toThrow();
  });

  it("still translates a leading inline flag", () => {
    expect(compilePySearch("(?i)stardock").test("STARDOCK")).toBe(true);
  });
});
