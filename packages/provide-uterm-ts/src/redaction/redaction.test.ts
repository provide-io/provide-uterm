//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { makeRedactor, redactText } from "./index.ts";

interface RedactionGolden {
  cases: Array<{ patterns: string[]; text: string; out: string; identity: string }>;
  dialect_divergences: Array<{ patterns: string[]; text: string; out: string }>;
}

const golden = loadGolden<RedactionGolden>("redaction_golden.json");

describe("makeRedactor", () => {
  it("returns the identity redactor when no patterns are configured", () => {
    const redactor = makeRedactor([]);
    expect(redactor("the secret value")).toBe("the secret value");
  });

  it("returns the identity redactor when the pattern list is omitted", () => {
    expect(makeRedactor()("the secret value")).toBe("the secret value");
  });

  it("replaces every occurrence of a pattern, not just the first", () => {
    expect(makeRedactor(["secret"])("secret secret")).toBe("[REDACTED] [REDACTED]");
  });

  it("leaves text alone when the pattern does not match", () => {
    expect(makeRedactor(["secret"])("no match here")).toBe("no match here");
  });

  it("applies patterns in order, each seeing the previous output", () => {
    expect(makeRedactor(["foo", "bar"])("foo bar baz")).toBe("[REDACTED] [REDACTED] baz");
  });

  it("lets a later pattern match the literal replacement text", () => {
    expect(makeRedactor(["foo", "\\[REDACTED\\]"])("foo bar")).toBe("[REDACTED] bar");
  });

  it("does not carry scanner state between calls", () => {
    const redactor = makeRedactor(["secret"]);
    expect(redactor("secret")).toBe(redactor("secret"));
  });

  it("does not treat the replacement as a substitution template", () => {
    // A naive implementation would let `$&` in the replacement re-expand.
    expect(makeRedactor(["(a)(b)"])("ab")).toBe("[REDACTED]");
  });

  it("translates a leading inline flag group into RegExp flags", () => {
    expect(makeRedactor(["(?i)secret"])("Secret SECRET")).toBe("[REDACTED] [REDACTED]");
    expect(makeRedactor(["(?m)^line"])("line one\nline two")).toBe("[REDACTED] one\n[REDACTED] two");
    expect(makeRedactor(["(?s)a.b"])("a\nb")).toBe("[REDACTED]");
    expect(makeRedactor(["(?im)^secret$"])("SECRET\nsecret")).toBe("[REDACTED]\n[REDACTED]");
  });

  it("leaves a non-leading inline flag group to the host engine", () => {
    // CPython rejects these outright; the host engine decides. What must not
    // happen is the leading-flag shim silently stripping them mid-pattern.
    expect(() => makeRedactor(["a(?i)b"])).toThrow(SyntaxError);
  });

  it("rejects an unsupported inline flag rather than silently dropping it", () => {
    expect(() => makeRedactor(["(?x) secret"])).toThrow(/unsupported inline regex flag/i);
  });

  it("propagates an invalid pattern as a compile error", () => {
    expect(() => makeRedactor(["("])).toThrow(SyntaxError);
  });
});

describe("redactText", () => {
  it("returns the text unchanged when no redactor is configured", () => {
    expect(redactText("the secret value", undefined)).toBe("the secret value");
    expect(redactText("the secret value", null)).toBe("the secret value");
  });

  it("applies the redactor when one is configured", () => {
    expect(redactText("the secret value", makeRedactor(["secret"]))).toBe("the [REDACTED] value");
  });
});

describe("differential parity with CPython", () => {
  it("matches every golden record", () => {
    for (const record of golden.cases) {
      expect(makeRedactor(record.patterns)(record.text)).toBe(record.out);
      expect(redactText(record.text, null)).toBe(record.identity);
    }
    expect(golden.cases.length).toBeGreaterThan(25);
  });

  it("documents where the host regex dialect diverges from CPython", () => {
    // Patterns are handed to the host engine unchanged, exactly as the Go and
    // C# ports do. CPython reads \d and \w as Unicode-aware for str subjects;
    // ECMAScript (like Go's RE2) reads them as ASCII-only. These records pin
    // the divergence so it is visible rather than discovered in production.
    const divergences = golden.dialect_divergences.map((record) => ({
      patterns: record.patterns,
      cpython: record.out,
      host: makeRedactor(record.patterns)(record.text),
    }));
    expect(divergences).toStrictEqual([
      { patterns: ["x\\w"], cpython: "[REDACTED] and [REDACTED]", host: "xé and [REDACTED]" },
      { patterns: ["\\d+"], cpython: "[REDACTED] and [REDACTED]", host: "[REDACTED] and ٤٢" },
      { patterns: ["\\w+"], cpython: "[REDACTED]", host: "[REDACTED]ï[REDACTED]" },
    ]);
  });
});
