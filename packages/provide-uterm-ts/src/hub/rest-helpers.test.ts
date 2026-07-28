//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  compileExpectRegex,
  extractPromptId,
  MAX_EXPECT_REGEX_LEN,
  PromptRegexError,
  snapshotMatches,
} from "./index.ts";

interface RestHelpersGolden {
  max_expect_regex_len: number;
  prompt_ids: Array<{ name: string; snapshot: Record<string, unknown> | null; prompt_id: string | null }>;
  matches: Array<{
    name: string;
    snapshot: Record<string, unknown> | null;
    expect_prompt_id: string | null;
    expect_regex: string | null;
    matched: boolean;
  }>;
  compiles: Array<{
    name: string;
    pattern: string | null;
    length: number | null;
    ok: boolean;
    is_none?: boolean;
    kind?: string;
    message?: string;
    max_length?: number | null;
  }>;
}

const golden = loadGolden<RestHelpersGolden>("rest_helpers_golden.json");

/** Compile a guard the way the poll loop does. */
function guard(source: string | null): RegExp | undefined {
  return source === null ? undefined : compileExpectRegex(source);
}

describe("extractPromptId", () => {
  it.each(golden.prompt_ids)("$name", (record) => {
    // Every rejection here is a shape a worker can actually send, so the
    // guard must decline rather than throw on any of them.
    expect(extractPromptId(record.snapshot ?? undefined)).toBe(record.prompt_id ?? undefined);
  });

  it("declines an empty id rather than returning it", () => {
    // An empty id would compare equal to an empty guard and match everything.
    const record = golden.prompt_ids.find((entry) => entry.name === "detection with an empty id");
    expect(record?.prompt_id).toBeNull();
  });
});

describe("snapshotMatches", () => {
  it.each(golden.matches)("$name", (record) => {
    expect(
      snapshotMatches(record.snapshot ?? undefined, {
        expectPromptId: record.expect_prompt_id ?? undefined,
        expectRegex: guard(record.expect_regex),
      }),
    ).toBe(record.matched);
  });

  it("never matches without a snapshot, even with no guards", () => {
    // "Nothing to check" is not "everything passed": the caller is waiting
    // for the worker to say something, not for it to stay silent.
    expect(golden.matches.find((entry) => entry.name === "no snapshot, no guards")?.matched).toBe(false);
    expect(snapshotMatches(undefined, {})).toBe(false);
  });

  it("treats an empty prompt id as no constraint", () => {
    const record = golden.matches.find((entry) => entry.name === "empty prompt id is not a constraint");
    expect(record?.matched).toBe(true);
  });

  it("requires both guards when both are given", () => {
    expect(golden.matches.find((entry) => entry.name === "only the prompt id holds")?.matched).toBe(false);
    expect(golden.matches.find((entry) => entry.name === "only the regex holds")?.matched).toBe(false);
    expect(golden.matches.find((entry) => entry.name === "both guards hold")?.matched).toBe(true);
  });

  it("stringifies a screen that is not a string", () => {
    const record = golden.matches.find((entry) => entry.name === "regex against a non-string screen");
    expect(record?.matched).toBe(true);
  });

  it("does not carry match position between calls", () => {
    // A guard compiled with the global flag would advance lastIndex and
    // report a miss on the very next poll of the same screen.
    const compiled = compileExpectRegex("ready") as RegExp;
    const snapshot = { screen: "ready" };
    expect(snapshotMatches(snapshot, { expectRegex: compiled })).toBe(true);
    expect(snapshotMatches(snapshot, { expectRegex: compiled })).toBe(true);
  });
});

describe("compileExpectRegex", () => {
  it.each(golden.compiles)("$name", (record) => {
    if (record.ok) {
      const compiled = compileExpectRegex(record.pattern ?? undefined);
      expect(compiled === undefined).toBe(record.is_none);
      return;
    }
    let thrown: unknown;
    try {
      compileExpectRegex(record.pattern ?? undefined);
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(PromptRegexError);
    expect((thrown as PromptRegexError).kind).toBe(record.kind);
    expect((thrown as PromptRegexError).maxLength).toBe(record.max_length ?? undefined);
  });

  it("checks the length before anything else", () => {
    // An over-long pattern must be refused before it reaches the ReDoS
    // validator, or a caller could spend the validator's time on a megabyte.
    const record = golden.compiles.find((entry) => entry.name === "unsafe but over-long");
    expect(record?.kind).toBe("too_long");
  });

  it("accepts a pattern exactly at the limit", () => {
    const record = golden.compiles.find((entry) => entry.name === "at the length limit");
    expect(record?.length).toBe(MAX_EXPECT_REGEX_LEN);
    expect(record?.ok).toBe(true);
  });

  it("exposes the reference limit", () => {
    expect(MAX_EXPECT_REGEX_LEN).toBe(golden.max_expect_regex_len);
  });

  it("takes an explicit limit", () => {
    expect(() => compileExpectRegex("abcdef", { maxLength: 3 })).toThrow(PromptRegexError);
    expect(compileExpectRegex("abc", { maxLength: 3 })).toBeInstanceOf(RegExp);
  });

  it("compiles case-insensitively and multiline, as the poll loop needs", () => {
    // Prompt text varies in case and a guard is written against one line of
    // a screen, not the whole buffer.
    const compiled = compileExpectRegex("^ready") as RegExp;
    expect(compiled.flags).toContain("i");
    expect(compiled.flags).toContain("m");
  });
});
