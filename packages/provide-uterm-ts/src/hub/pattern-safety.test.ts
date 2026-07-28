//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { UnsafePatternError, validatePatternSafety } from "./index.ts";

interface PatternSafetyGolden {
  patterns: Array<{ name: string; pattern: string; safe: boolean; error: string | null }>;
}

const golden = loadGolden<PatternSafetyGolden>("pattern_safety_golden.json");

describe("validatePatternSafety", () => {
  it.each(golden.patterns)("$name", (record) => {
    // The guard this protects is searched against a whole screen inside the
    // hijack poll loop, so a backtracking pattern is not a slow query — it
    // stalls every other session on the hub.
    if (record.safe) {
      expect(() => validatePatternSafety(record.pattern)).not.toThrow();
      return;
    }
    expect(() => validatePatternSafety(record.pattern)).toThrow(UnsafePatternError);
    expect(() => validatePatternSafety(record.pattern)).toThrow(record.error ?? "");
  });

  it("rejects a repeat count written in non-ASCII digits", () => {
    // The bypass an ASCII-only digit test would open: the brace stops
    // registering as a quantifier, so the nested-quantifier rule never fires.
    for (const name of ["arabic-indic digit count", "superscript digit count", "fullwidth digit count"]) {
      const record = golden.patterns.find((entry) => entry.name === name);
      expect(record?.safe).toBe(false);
      expect(() => validatePatternSafety(record?.pattern ?? "")).toThrow(UnsafePatternError);
    }
  });

  it("distinguishes the two refusal reasons", () => {
    // Callers surface these, and they describe genuinely different mistakes.
    expect(() => validatePatternSafety("(a+)+")).toThrow("nested quantified groups");
    expect(() => validatePatternSafety("(a|b)+")).toThrow("quantified groups containing alternation");
  });

  it("does not treat a brace it cannot parse as a quantifier", () => {
    // Otherwise every literal brace would start rejecting valid patterns.
    expect(() => validatePatternSafety("(a+){foo}")).not.toThrow();
    expect(() => validatePatternSafety("(a+){}")).not.toThrow();
    expect(() => validatePatternSafety("(a+){2")).not.toThrow();
  });

  it("ignores metacharacters inside a character class", () => {
    expect(() => validatePatternSafety("[(a+)]+")).not.toThrow();
    expect(() => validatePatternSafety("[|]+")).not.toThrow();
  });

  it("ignores escaped metacharacters", () => {
    expect(() => validatePatternSafety(String.raw`\(a+\)+`)).not.toThrow();
    expect(() => validatePatternSafety(String.raw`(a+)\+`)).not.toThrow();
  });

  it("forgets a group once a literal follows it", () => {
    // The quantifier has to apply to the group itself; a literal in between
    // means the repeat is of the literal and carries no nesting risk.
    expect(() => validatePatternSafety("(a+)x+")).not.toThrow();
  });
});
