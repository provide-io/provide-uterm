//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { prepareKeystrokes, sanitizeKeystrokes, unescapeKeys } from "./index.ts";

interface SanitizerGolden {
  unescape: Array<{ raw: string; out: string }>;
  sanitize: Array<{ keys: string; max_bytes: number; out: string }>;
  prepare: Array<{ raw: string; max_bytes: number; out: string }>;
}

const golden = loadGolden<SanitizerGolden>("sanitizer_golden.json");

describe("unescapeKeys", () => {
  it("leaves text with no escapes unchanged", () => {
    expect(unescapeKeys("plain")).toBe("plain");
    expect(unescapeKeys("")).toBe("");
  });

  it.each([
    ["\\n", "\n"],
    ["\\r", "\r"],
    ["\\t", "\t"],
    ["\\e", "\x1b"],
    ["\\0", "\x00"],
    ["\\\\", "\\"],
    ["\\'", "'"],
    ['\\"', '"'],
  ])("translates the simple escape %j", (raw, want) => {
    expect(unescapeKeys(raw)).toBe(want);
  });

  it("translates two-digit hex escapes in either case", () => {
    expect(unescapeKeys("\\x1b")).toBe("\x1b");
    expect(unescapeKeys("\\x1B")).toBe("\x1b");
  });

  it("translates four-digit unicode escapes", () => {
    expect(unescapeKeys("\\u0041")).toBe("A");
    expect(unescapeKeys("\\u00e9")).toBe("é");
  });

  it("leaves a malformed hex escape verbatim", () => {
    expect(unescapeKeys("\\x1")).toBe("\\x1");
    expect(unescapeKeys("\\xzz")).toBe("\\xzz");
  });

  it("leaves an unknown single-character escape verbatim", () => {
    expect(unescapeKeys("\\q")).toBe("\\q");
  });

  it("leaves a trailing lone backslash verbatim", () => {
    expect(unescapeKeys("abc\\")).toBe("abc\\");
  });

  it("escapes a newline, because the pattern is DOTALL", () => {
    expect(unescapeKeys("\\\n")).toBe("\\\n");
  });

  it("does not let an escaped backslash escape the next character", () => {
    expect(unescapeKeys("\\\\n")).toBe("\\n");
  });
});

describe("sanitizeKeystrokes", () => {
  it("keeps printable ASCII", () => {
    expect(sanitizeKeystrokes("hello world")).toBe("hello world");
  });

  it("keeps the terminal input controls", () => {
    expect(sanitizeKeystrokes("a\rb\nc\td")).toBe("a\rb\nc\td");
    expect(sanitizeKeystrokes("\x03")).toBe("\x03");
    expect(sanitizeKeystrokes("\x1b[A")).toBe("\x1b[A");
    expect(sanitizeKeystrokes("\x0b\x0c")).toBe("\x0b\x0c");
  });

  it("drops other C0 controls", () => {
    expect(sanitizeKeystrokes("\x00\x01\x07bell")).toBe("bell");
  });

  it("drops DEL and the C1 range", () => {
    expect(sanitizeKeystrokes("\x7f\x80\x9f")).toBe("");
  });

  it("drops non-ASCII characters, which are not in string.printable", () => {
    expect(sanitizeKeystrokes("aéb")).toBe("ab");
    expect(sanitizeKeystrokes("你好")).toBe("");
  });

  it("truncates to the byte budget", () => {
    expect(sanitizeKeystrokes("abcdefghij", 4)).toBe("abcd");
  });

  it("returns the whole string when it is exactly at the budget", () => {
    expect(sanitizeKeystrokes("abcdefghij", 10)).toBe("abcdefghij");
  });

  it("applies the byte budget after filtering, not before", () => {
    // "aébcdefghijk" filters to "abcdefghijk"; the budget then keeps 5.
    expect(sanitizeKeystrokes("aébcdefghijk", 5)).toBe("abcde");
  });

  it("defaults the byte budget to 4096", () => {
    const long = "a".repeat(5000);
    expect(sanitizeKeystrokes(long)).toHaveLength(4096);
  });
});

describe("prepareKeystrokes", () => {
  it("unescapes before sanitizing", () => {
    expect(prepareKeystrokes("ls -la\\r")).toBe("ls -la\r");
  });

  it("drops a control produced by unescaping when it is not allowed", () => {
    expect(prepareKeystrokes("\\x07bell")).toBe("bell");
  });

  it("applies the byte budget to the unescaped text", () => {
    expect(prepareKeystrokes("\\x41\\x42\\x43", 2)).toBe("AB");
  });
});

describe("differential parity with CPython", () => {
  it("matches every unescape golden record", () => {
    for (const record of golden.unescape) {
      expect(unescapeKeys(record.raw)).toBe(record.out);
    }
    expect(golden.unescape.length).toBeGreaterThan(30);
  });

  it("matches every sanitize golden record", () => {
    for (const record of golden.sanitize) {
      expect(sanitizeKeystrokes(record.keys, record.max_bytes)).toBe(record.out);
    }
    expect(golden.sanitize.length).toBeGreaterThan(15);
  });

  it("matches every prepare golden record", () => {
    for (const record of golden.prepare) {
      expect(prepareKeystrokes(record.raw, record.max_bytes)).toBe(record.out);
    }
    expect(golden.prepare.length).toBeGreaterThan(5);
  });
});
