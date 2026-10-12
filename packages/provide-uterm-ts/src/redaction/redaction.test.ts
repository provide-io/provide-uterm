//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  DEPTH_LIMIT_MARKER,
  isSensitiveKey,
  makeRedactor,
  REDACT_MAX_DEPTH,
  redactStructure,
  redactText,
  SENSITIVE_KEY_MARKER,
  keySegments,
  NON_SECRET_KEY_SUFFIXES,
  SENSITIVE_KEY_PAIRS,
  SENSITIVE_KEY_SEGMENTS,
} from "./index.ts";

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

describe("redactStructure", () => {
  const r = makeRedactor(["S3cret\\w*"]);

  function nest(depth: number, leaf: unknown, kind: "object" | "array"): unknown {
    let value = leaf;
    for (let i = 0; i < depth; i++) {
      value = kind === "object" ? { k: value } : [value];
    }
    return value;
  }

  function innermost(value: unknown): unknown {
    let cur = value;
    while (typeof cur === "object" && cur !== null) {
      cur = Array.isArray(cur) ? cur[0] : (cur as Record<string, unknown>).k;
    }
    return cur;
  }

  it("pins the constants shared with the reference", () => {
    expect(REDACT_MAX_DEPTH).toBe(32);
    expect(DEPTH_LIMIT_MARKER).toBe("[NESTED_REDACTED]");
    expect(SENSITIVE_KEY_MARKER).toBe("[SECRET_REDACTED]");
    expect([...SENSITIVE_KEY_SEGMENTS].sort()).toStrictEqual([
      "apikey",
      "authorization",
      "cookie",
      "credential",
      "credentials",
      "passphrase",
      "passwd",
      "password",
      "secret",
      "token",
    ]);
    expect(SENSITIVE_KEY_PAIRS).toStrictEqual([
      ["api", "key"],
      ["private", "key"],
      ["access", "key"],
    ]);
    expect([...NON_SECRET_KEY_SUFFIXES].sort()).toStrictEqual([
      "count",
      "enabled",
      "expires",
      "expiry",
      "hash",
      "kind",
      "len",
      "length",
      "required",
      "size",
      "ttl",
      "type",
    ]);
  });

  it("redacts strings at every level and keeps other scalars", () => {
    expect(redactStructure("a S3cretX", r)).toBe("a [REDACTED]");
    expect(redactStructure({ a: ["S3cret1", { b: "S3cret2" }, 1, true, null] }, r)).toStrictEqual({
      a: ["[REDACTED]", { b: "[REDACTED]" }, 1, true, null],
    });
    expect(redactStructure(undefined, r)).toBeUndefined();
  });

  it("never mutates its input", () => {
    const input = { a: ["S3cret1"] };
    const out = redactStructure(input, r) as { a: string[] };
    expect(input).toStrictEqual({ a: ["S3cret1"] });
    expect(out.a).not.toBe(input.a);
  });

  it.each(["object", "array"] as const)("walks the last level inside the cap (%s)", (kind) => {
    expect(innermost(redactStructure(nest(REDACT_MAX_DEPTH, "S3cretLeaf", kind), r))).toBe("[REDACTED]");
  });

  it.each(["object", "array"] as const)("replaces a container at the cap with the marker (%s)", (kind) => {
    const out = redactStructure(nest(REDACT_MAX_DEPTH + 1, "S3cretLeaf", kind), r);
    expect(innermost(out)).toBe(DEPTH_LIMIT_MARKER);
    expect(JSON.stringify(out)).not.toContain("S3cretLeaf");
  });

  it("honours an explicit maxDepth", () => {
    expect(redactStructure({ a: { b: "x" } }, r, { maxDepth: 1 })).toStrictEqual({ a: DEPTH_LIMIT_MARKER });
    expect(redactStructure(["x"], r, { maxDepth: 0 })).toBe(DEPTH_LIMIT_MARKER);
  });

  it("survives a structure far deeper than the stack", () => {
    const out = redactStructure(nest(100_000, "S3cretLeaf", "object"), r, { keyAware: true });
    expect(innermost(out)).toBe(DEPTH_LIMIT_MARKER);
  });

  it("masks sensitive keys only when key-aware, and keeps a null credential null", () => {
    const frame = { type: "resume", token: "abc", wrap: [{ resume_token: "x", password: null, keep: "S3cret" }] };
    expect(redactStructure(frame, r)).toStrictEqual({
      type: "resume",
      token: "abc",
      wrap: [{ resume_token: "x", password: null, keep: "[REDACTED]" }],
    });
    expect(redactStructure(frame, r, { keyAware: true })).toStrictEqual({
      type: "resume",
      token: SENSITIVE_KEY_MARKER,
      wrap: [{ resume_token: SENSITIVE_KEY_MARKER, password: null, keep: "[REDACTED]" }],
    });
    expect(redactStructure({ token: undefined }, r, { keyAware: true })).toStrictEqual({ token: undefined });
    expect(redactStructure({ secret: { a: 1 } }, r, { keyAware: true })).toStrictEqual({
      secret: SENSITIVE_KEY_MARKER,
    });
  });

  it.each([
    "token",
    "TOKEN",
    "Resume-Token",
    "session_token",
    "passwd",
    "ssh_passphrase",
    "client_secret",
    "api-key",
    "apikey",
    "openai_api_key",
    "Authorization",
    "cookie",
    "private_key",
    "db_password",
    "accessToken",
    "clientSecret",
    "privateKey",
    "Set-Cookie",
    "Proxy-Authorization",
    "credentials",
    "aws_secret_access_key",
    "APIKey",
    "x-api-key",
    "access_key_id",
    "HTTPCookie",
    "refresh.token",
    "Session Token",
  ])("treats %s as sensitive", (key) => {
    expect(isSensitiveKey(key)).toBe(true);
  });

  it.each([
    "",
    "_",
    "tokens",
    "token_count",
    "max_tokens",
    "secretary",
    "type",
    "tokenizer",
    "token_type",
    "password_required",
    "secret_hash",
    "session_id",
    "api",
    "keyApi",
    "TOKEN_TTL",
    "token2",
  ])("treats %s as ordinary", (key) => {
    expect(isSensitiveKey(key)).toBe(false);
  });

  it.each([
    ["accessToken", ["access", "token"]],
    ["APIKey", ["api", "key"]],
    ["HTTPCookie", ["http", "cookie"]],
    ["RESUME_TOKEN", ["resume", "token"]],
    ["a.b c-d__e", ["a", "b", "c", "d", "e"]],
    ["token2Fa", ["token2", "fa"]],
    ["aB", ["a", "b"]],
    ["AB", ["ab"]],
    ["ABc", ["a", "bc"]],
    ["", []],
  ])("splits %s into segments", (key, segments) => {
    expect(keySegments(key as string)).toStrictEqual(segments);
  });

  it("passes non-plain objects through unchanged instead of flattening them", () => {
    class Point {
      constructor(readonly x: string) {}
    }
    const date = new Date(0);
    const map = new Map([["k", "S3cret"]]);
    const set = new Set(["S3cret"]);
    const point = new Point("S3cret");
    const out = redactStructure(
      { date, map, set, point, bare: Object.assign(Object.create(null), { s: "S3cret" }) },
      r,
    ) as Record<string, unknown>;
    expect(out.date).toBe(date);
    expect(out.map).toBe(map);
    expect(out.set).toBe(set);
    expect(out.point).toBe(point);
    expect(out.bare).toStrictEqual({ s: "[REDACTED]" });
  });
});
