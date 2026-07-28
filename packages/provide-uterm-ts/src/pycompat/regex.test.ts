//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { compilePyPattern } from "./index.ts";

describe("compilePyPattern", () => {
  it("compiles a plain pattern with the global flag", () => {
    expect(compilePyPattern("secret").flags).toBe("g");
  });

  it("preserves the pattern source when there is no inline flag group", () => {
    expect(compilePyPattern("a.b").source).toBe("a.b");
  });

  it.each([
    ["(?i)", "gi"],
    ["(?m)", "gm"],
    ["(?s)", "gs"],
    ["(?im)", "gim"],
    ["(?msi)", "gims"],
  ])("translates the leading inline group %s into RegExp flags", (group, want) => {
    const compiled = compilePyPattern(`${group}secret`);
    expect(compiled.flags.split("").sort().join("")).toBe(want.split("").sort().join(""));
    expect(compiled.source).toBe("secret");
  });

  it("never emits a duplicate flag for a repeated inline flag letter", () => {
    expect(compilePyPattern("(?ii)x").flags).toBe("gi");
  });

  it("rejects an inline flag with no ECMAScript equivalent", () => {
    expect(() => compilePyPattern("(?x) secret")).toThrow(/unsupported inline regex flag: x/i);
    expect(() => compilePyPattern("(?a)secret")).toThrow(/unsupported inline regex flag: a/i);
  });

  it("leaves a non-leading inline flag group for the host engine to reject", () => {
    expect(() => compilePyPattern("a(?i)b")).toThrow(SyntaxError);
  });

  it("does not mistake a non-flag group at the start for inline flags", () => {
    expect(compilePyPattern("(?:abc)").source).toBe("(?:abc)");
    expect(compilePyPattern("(?=abc)").source).toBe("(?=abc)");
    expect(compilePyPattern("(?<name>abc)").source).toBe("(?<name>abc)");
  });

  it("returns a fresh RegExp per call so lastIndex never leaks", () => {
    const pattern = "a";
    const first = compilePyPattern(pattern);
    first.exec("aa");
    expect(first.lastIndex).toBe(1);
    expect(compilePyPattern(pattern).lastIndex).toBe(0);
  });

  it("propagates an invalid pattern as a compile error", () => {
    expect(() => compilePyPattern("(")).toThrow(SyntaxError);
  });
});
