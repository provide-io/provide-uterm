//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The redactor a recording runs through when `redact_sensitive` is on.
 *
 * Every expected value comes from `stream_redaction_golden.json`, which
 * `gen_stream_redaction_golden.py` records off the reference's own
 * `StreamRedactor(default_rules())`.
 */

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { defaultRedactionRules, type RedactionRule, StreamRedactor } from "./index.ts";

interface Case {
  input: string;
  output: string;
}

interface StreamRedactionGolden {
  default_rules: RedactionRule[];
  default: Case[];
  custom_rules: RedactionRule[];
  custom: Case[];
  uniform_rules: RedactionRule[];
  uniform: Case[];
  empty: Case[];
}

const golden = loadGolden<StreamRedactionGolden>("stream_redaction_golden.json");

describe("defaultRedactionRules", () => {
  it("is the reference's rule set, pattern for pattern and marker for marker", () => {
    expect(defaultRedactionRules()).toStrictEqual(golden.default_rules);
  });

  it("hands out a fresh list, so one caller extending it cannot change another's", () => {
    const first = defaultRedactionRules();
    first.push({ pattern: "x", replacement: "y" });
    expect(defaultRedactionRules()).toHaveLength(golden.default_rules.length);
  });
});

describe("StreamRedactor", () => {
  it.each(golden.default)("redacts %j as the reference does with the default rules", ({ input, output }) => {
    expect(new StreamRedactor(defaultRedactionRules()).redact(input)).toBe(output);
  });

  it.each(golden.custom)("picks the replacement of the rule that owns the match in %j", ({ input, output }) => {
    expect(new StreamRedactor(golden.custom_rules).redact(input)).toBe(output);
  });

  it.each(golden.uniform)("uses the one shared replacement for %j", ({ input, output }) => {
    expect(new StreamRedactor(golden.uniform_rules).redact(input)).toBe(output);
  });

  it.each(golden.empty)("leaves %j alone with no rules", ({ input, output }) => {
    expect(new StreamRedactor([]).redact(input)).toBe(output);
    expect(new StreamRedactor().redact(input)).toBe(output);
  });

  it("defaults a rule's replacement to the reference's marker", () => {
    expect(new StreamRedactor([{ pattern: "secret" }]).redact("a secret")).toBe("a [REDACTED]");
  });

  it("skips a rule that will not compile, as the reference skips an re.error", () => {
    const redactor = new StreamRedactor([{ pattern: "(unclosed" }, { pattern: "secret", replacement: "<S>" }]);
    expect(redactor.redact("(unclosed secret")).toBe("(unclosed <S>");
  });

  it("is the identity when every rule fails to compile", () => {
    expect(new StreamRedactor([{ pattern: "[" }]).redact("[ text")).toBe("[ text");
  });

  it("can be called repeatedly without one call's position leaking into the next", () => {
    const redactor = new StreamRedactor([{ pattern: "a" }]);
    expect(redactor.redact("a")).toBe("[REDACTED]");
    expect(redactor.redact("a")).toBe("[REDACTED]");
  });

  it("is usable detached from its instance, as a recorder's redactor is", () => {
    const { redact } = new StreamRedactor([{ pattern: "a", replacement: "b" }]);
    expect(redact("aa")).toBe("bb");
  });
});
