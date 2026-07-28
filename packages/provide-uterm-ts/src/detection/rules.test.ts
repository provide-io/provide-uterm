//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { compilePySearch } from "../pycompat/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { type MatchMode, parseRuleSet, RuleValidationError, regexRuleToRegex, toPromptPatterns } from "./index.ts";

interface RulesGolden {
  minimal_input: Record<string, unknown>;
  minimal_dump: Record<string, unknown>;
  minimal_patterns: Array<Record<string, unknown>>;
  full_input: Record<string, unknown>;
  full_dump: Record<string, unknown>;
  full_patterns: Array<Record<string, unknown>>;
  empty_dump: Record<string, unknown>;
  empty_patterns: Array<Record<string, unknown>>;
  regex_modes: Array<{ name: string; mode: MatchMode; pattern: string; regex: string }>;
  invalid: Array<{ name: string; payload: unknown; error: string }>;
  coerced: Array<{ name: string; payload: unknown; dump: Record<string, unknown> }>;
  default_flags: number;
}

const golden = loadGolden<RulesGolden>("rules_golden.json");

/** The pattern a mode and text resolve to. */
function resolved(mode: MatchMode, pattern: string): string {
  return regexRuleToRegex({ pattern, match_mode: mode, flags: golden.default_flags });
}

describe("reading a rule set", () => {
  it("fills in everything a minimal one leaves out", () => {
    // The defaults are a contract: every rule file already written against
    // the reference means what these say it means.
    expect(parseRuleSet(golden.minimal_input)).toStrictEqual(golden.minimal_dump);
  });

  it("reads a fully-specified one unchanged", () => {
    expect(parseRuleSet(golden.full_input)).toStrictEqual(golden.full_dump);
  });

  it("accepts a rule set with nothing in it", () => {
    // A target with no rules yet is a legitimate starting point.
    expect(parseRuleSet({ game: "none" })).toStrictEqual(golden.empty_dump);
  });

  it("defaults the version rather than demanding one", () => {
    expect(golden.minimal_dump.version).toBe("1.0");
  });

  it("reads a default action a prompt carries", () => {
    // An answer the rule already knows, so a flow need not spell out what to
    // send at a prompt that only takes one thing.
    const confirm = (golden.full_dump.prompts as Array<Record<string, unknown>>)[2];
    expect((confirm?.default_action as Record<string, unknown>)?.keys).toBe("Y");
    expect((golden.full_dump.prompts as Array<Record<string, unknown>>)[0]?.default_action).toBeNull();
  });

  it("takes an explicit null the same way as an omission", () => {
    // Both mean "there is none". A port handling only the omission would
    // refuse a file the reference takes.
    const confirm = (golden.full_dump.prompts as Array<Record<string, unknown>>)[2];
    expect(confirm?.negative_match).toBeNull();
    const menus = golden.full_dump.menus as Array<Record<string, unknown>>;
    expect(menus[0]?.title_match).toBeNull();
    expect(menus[1]?.title_match).not.toBeNull();
  });

  it("keeps whatever metadata it was given", () => {
    // Not part of detection, but it is how a rules file identifies itself.
    expect((golden.full_dump.metadata as Record<string, unknown>).revision).toBe(7);
    expect(golden.minimal_dump.metadata).toStrictEqual({});
  });

  it("defaults the extraction flags to multiline and case-insensitive", () => {
    // The number travels with the rule for whoever runs the extraction later,
    // so it is part of the wire format rather than a local detail.
    expect(golden.default_flags).toBe(10);
    const prompt = (golden.full_dump.prompts as Array<Record<string, unknown>>)[0];
    const extract = (prompt?.kv_extract as Array<Record<string, unknown>>)[0];
    expect(extract?.flags).toBe(golden.default_flags);
  });

  it("reads the extraction rule's validate under its written name", () => {
    // The field is spelled `validate` in the file and cannot be called that
    // in the reference's own model; a port reading the internal name instead
    // would silently drop every validation an operator wrote.
    const prompt = (golden.full_dump.prompts as Array<Record<string, unknown>>)[0];
    const extract = (prompt?.kv_extract as Array<Record<string, unknown>>)[1];
    expect(extract?.validate_rule).toStrictEqual({ min: 0 });
    expect((prompt?.kv_extract as Array<Record<string, unknown>>)[0]?.validate_rule).toBeNull();
    // ...and back out again under the written name, because the extractor
    // reads `validate`.
    expect((golden.full_patterns[0]?.kv_extract as Array<Record<string, unknown>>)[1]?.validate).toStrictEqual({
      min: 0,
    });
  });
});

describe("a rule set the reference refuses", () => {
  it.each(golden.invalid)("$name", (record) => {
    // Refused rather than guessed at. A rule that looked loaded and never
    // fired is the failure an operator cannot see.
    expect(() => parseRuleSet(record.payload)).toThrow(RuleValidationError);
  });

  it("refuses a closed set it does not recognise", () => {
    // The input type, the kind and the match mode are all closed. Accepting
    // an unknown one leaves a rule that is loaded and inert.
    for (const name of [
      "an unknown input type",
      "an unknown prompt kind",
      "an unknown match mode",
      "an unknown action kind",
    ]) {
      expect(golden.invalid.find((entry) => entry.name === name)).toBeDefined();
    }
  });

  it("refuses a sub-object that is not one", () => {
    // None of these carry a required field, so a port that skipped the shape
    // check would accept them and quietly use every default rather than
    // telling the operator their file is wrong.
    for (const name of [
      "a screen constraint that is a string",
      "a screen constraint that is a list",
      "a timing block that is a string",
      "a timing block that is a list",
      "a match block that is a string",
    ]) {
      const record = golden.invalid.find((entry) => entry.name === name);
      expect(() => parseRuleSet(record?.payload)).toThrow(RuleValidationError);
    }
  });

  it("refuses a gate prompt that is not a string", () => {
    const record = golden.invalid.find((entry) => entry.name === "a gate prompt that is not a string");
    expect(() => parseRuleSet(record?.payload)).toThrow(RuleValidationError);
  });

  it("refuses a value that is not a boolean", () => {
    // The cursor expectation and the stability flag are gates. A truthy
    // string read as one would silently invert what a rule asked for.
    expect(() =>
      parseRuleSet({ game: "g", prompts: [{ id: "a", match: { pattern: "x" }, screen: { expect_cursor_at_end: "yes" } }] }),
    ).toThrow(RuleValidationError);
    expect(() =>
      parseRuleSet({
        game: "g",
        prompts: [{ id: "a", match: { pattern: "x" }, kv_extract: [{ field: "f", regex: "r", required: 1 }] }],
      }),
    ).toThrow(RuleValidationError);
  });

  it("does not stringify a number into a text field", () => {
    // Coercion runs one way only. Stringifying these would turn a typo into a
    // version or an extraction type that nothing downstream recognises.
    for (const name of ["a version that is a number", "an extraction type that is a number"]) {
      const record = golden.invalid.find((entry) => entry.name === name);
      expect(() => parseRuleSet(record?.payload)).toThrow(RuleValidationError);
    }
  });

  it("names the source in the message", () => {
    // An operator with twenty rule files needs to know which one.
    expect(() => parseRuleSet({ prompts: [] }, "rules/tw2002.json")).toThrow(/rules\/tw2002\.json/);
  });

  it("says what was wrong with it", () => {
    let message = "";
    try {
      parseRuleSet({ game: "g", prompts: [{ id: "a", match: { pattern: "x" }, input_type: "chord" }] });
    } catch (error) {
      message = (error as Error).message;
    }
    expect(message).toContain("input_type");
  });
});

describe("a rule set the reference accepts by coercing", () => {
  it.each(golden.coerced)("$name", (record) => {
    // The reference's validator runs lax, where "5" is 5. A rules file that
    // quotes its numbers still loads there, so refusing them here would
    // reject files the reference takes.
    expect(parseRuleSet(record.payload)).toStrictEqual(record.dump);
  });

  it("reads a quoted number as a number", () => {
    const record = golden.coerced.find((entry) => entry.name === "a quoted cursor row");
    const prompt = (record?.dump.prompts as Array<Record<string, unknown>>)[0];
    expect((prompt?.screen as Record<string, unknown>).cursor_row_min).toBe(5);
  });

  it("still refuses a string that is not a number", () => {
    expect(() =>
      parseRuleSet({ game: "g", prompts: [{ id: "a", match: { pattern: "x" }, screen: { cursor_row_min: "left" } }] }),
    ).toThrow(RuleValidationError);
    expect(() =>
      parseRuleSet({ game: "g", prompts: [{ id: "a", match: { pattern: "x" }, screen: { cursor_row_min: "" } }] }),
    ).toThrow(RuleValidationError);
  });
});

describe("how literally a pattern is read", () => {
  it.each(golden.regex_modes)("$name", (record) => {
    expect(resolved(record.mode, record.pattern)).toBe(record.regex);
  });

  it("leaves a regex as the author wrote it", () => {
    expect(resolved("regex", "Command \\[TL=[\\d:]+\\]:")).toBe("Command \\[TL=[\\d:]+\\]:");
  });

  it("escapes a contains pattern so it means itself", () => {
    // Unescaped, "Command [TL=" is an unterminated character class — the rule
    // would not compile at all, or would match something else entirely.
    const escaped = resolved("contains", "Command [TL=");
    expect(compilePySearch(escaped).test("Command [TL=00:00:00]:")).toBe(true);
    expect(compilePySearch(escaped).test("Command X")).toBe(false);
  });

  it("anchors an exact pattern to a whole line", () => {
    // The difference between "this line is exactly this" and "this appears
    // somewhere on the screen".
    const escaped = resolved("exact", "Command [TL=00:00:00]:");
    expect(compilePySearch(escaped).test("Command [TL=00:00:00]:")).toBe(true);
    expect(compilePySearch(escaped).test("> Command [TL=00:00:00]: <")).toBe(false);
  });

  it("escapes every metacharacter a contains pattern might carry", () => {
    const record = golden.regex_modes.find((entry) => entry.name === "contains with every metacharacter");
    expect(compilePySearch(record?.regex as string).test(record?.pattern as string)).toBe(true);
  });

  it("copes with an empty pattern in each mode", () => {
    expect(resolved("regex", "")).toBe("");
    expect(resolved("contains", "")).toBe("");
    expect(resolved("exact", "")).toBe("^$");
  });
});

describe("turning rules into detector patterns", () => {
  it("produces what the reference produces", () => {
    expect(toPromptPatterns(parseRuleSet(golden.full_input))).toStrictEqual(golden.full_patterns);
    expect(toPromptPatterns(parseRuleSet(golden.minimal_input))).toStrictEqual(golden.minimal_patterns);
    expect(toPromptPatterns(parseRuleSet({ game: "none" }))).toStrictEqual(golden.empty_patterns);
  });

  it("carries the screen constraint through as the detector spells it", () => {
    // The rule says expect_cursor_at_end under `screen`; the detector reads it
    // at the top level. A port that forgot to move it would make every rule
    // demand the cursor.
    const pause = golden.full_patterns.find((pattern) => pattern.id === "pause");
    expect(pause?.expect_cursor_at_end).toBe(false);
    expect(golden.full_patterns.find((pattern) => pattern.id === "login")?.expect_cursor_at_end).toBe(true);
  });

  it("resolves the match mode before handing it over", () => {
    // The detector compiles what it is given, so the escaping has to have
    // happened by now.
    expect(golden.full_patterns.find((pattern) => pattern.id === "pause")?.regex).toBe("press\\ any\\ key");
    expect(golden.full_patterns.find((pattern) => pattern.id === "confirm")?.regex).toBe("^\\(Y/N\\)\\?$");
  });

  it("resolves an exclusion the same way", () => {
    // The exclusion carries characters that have to be escaped, so a port
    // that handed the raw pattern over would compile a different expression.
    expect(golden.full_patterns.find((pattern) => pattern.id === "pause")?.negative_regex).toBe(
      "STARDOCK\\ \\(closed\\)",
    );
  });

  it("leaves out an exclusion a rule did not write", () => {
    // Present-but-empty and absent are different to the detector: it reads
    // the key by presence.
    expect(Object.hasOwn(golden.full_patterns[0] as object, "negative_regex")).toBe(false);
    expect(Object.hasOwn(golden.minimal_patterns[0] as object, "negative_regex")).toBe(false);
  });

  it("leaves out extraction rules a prompt did not write", () => {
    expect(Object.hasOwn(golden.minimal_patterns[0] as object, "kv_extract")).toBe(false);
    expect(Object.hasOwn(golden.full_patterns[0] as object, "kv_extract")).toBe(true);
  });

  it("carries the extraction rules with their flags and validation", () => {
    const extract = golden.full_patterns[0]?.kv_extract as Array<Record<string, unknown>>;
    expect(extract[0]).toStrictEqual({
      field: "sector",
      regex: "Sector\\s+(\\d+)",
      type: "int",
      flags: golden.default_flags,
      validate: null,
      required: true,
    });
    expect(extract[1]?.validate).toStrictEqual({ min: 0 });
    expect(extract[1]?.required).toBe(false);
  });

  it("marks every pattern as hand-written", () => {
    // The flag tells a later learning pass which rules a human wrote and must
    // not be overwritten.
    for (const pattern of golden.full_patterns) {
      expect(pattern.auto_detected).toBe(false);
    }
  });

  it("carries notes as text rather than nothing", () => {
    // The detector's own diagnostics print them; null would render as the
    // word "null" in an operator's log.
    expect(golden.full_patterns.find((pattern) => pattern.id === "login")?.notes).toBe("the first thing it asks");
    expect(golden.minimal_patterns[0]?.notes).toBe("");
  });

  it("keeps the prompts in the order they were written", () => {
    // Rule order is the author's priority and the detector honours it, so a
    // reordering here silently changes which prompt wins.
    expect(golden.full_patterns.map((pattern) => pattern.id)).toStrictEqual(["login", "pause", "confirm"]);
  });

  it("does not turn menus or flows into prompts", () => {
    // They are recognised separately; folding them in would make a menu title
    // a prompt that answers itself.
    expect(golden.full_patterns).toHaveLength(3);
  });
});
