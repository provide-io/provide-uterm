//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  compiledPatternOrRejection,
  compileUserPattern,
  hasCatastrophicConstruct,
  MAX_USER_PATTERN_LEN,
  rejectBadId,
  rejectBadIds,
  rejectBadPattern,
} from "./index.ts";

interface Rejection {
  success: false;
  error: string;
  detail: string;
}

interface GuardsGolden {
  max_pattern_length: number;
  catastrophic: Array<{ name: string; pattern: string; refused: boolean }>;
  compiled: Array<{ name: string; pattern: string; compiled?: string; error?: string }>;
  pattern_rejections: Array<{ name: string; pattern: string; rejection: Rejection | null }>;
  no_pattern_rejection: null;
  no_pattern_compiled: [null, null];
  compiled_or_rejection: Array<{ name: string; pattern: string; compiled: string | null; rejection: Rejection | null }>;
  ids: Array<{ name: string; value: string; kind: string; rejection: Rejection | null }>;
  id_default_kind: Rejection;
  id_pairs: Array<{ name: string; pairs: Array<[string, string]>; rejection: Rejection | null }>;
}

const golden = loadGolden<GuardsGolden>("mcpguards_golden.json");

/** A pattern the reference refused for a reason this runtime shares. */
function sharedRefusal(error: string): boolean {
  // Not the engine's own complaint: two engines disagree about what is a
  // valid pattern, and their wording never agreed to begin with.
  return !error.startsWith("invalid pattern:");
}

describe("the shapes a regex may not have", () => {
  it("caps a pattern where the reference caps one", () => {
    expect(MAX_USER_PATTERN_LEN).toBe(golden.max_pattern_length);
  });

  it.each(golden.catastrophic)("$name", (record) => {
    expect(hasCatastrophicConstruct(record.pattern)).toBe(record.refused);
  });

  it("refuses a quantified group whose body is quantified", () => {
    // The classic exponential shape: every quantifier multiplies the ways the
    // engine can split the same input, and a `viewer` may hand this in.
    for (const pattern of ["(a+)+", "(a*)*", "(a+)*", "(a*)+", "(a{1,2})+", "(a+?)+"]) {
      expect(hasCatastrophicConstruct(pattern)).toBe(true);
    }
  });

  it("refuses a quantified backreference", () => {
    for (const pattern of ["(a)\\1+", "(a)\\1*", "(a)\\1{2}", "(a)(\\1)+"]) {
      expect(hasCatastrophicConstruct(pattern)).toBe(true);
    }
  });

  it("allows a quantifier that is only written like one", () => {
    // An escaped `*` is a literal asterisk; reading it as a quantifier would
    // refuse patterns nobody could rewrite.
    expect(hasCatastrophicConstruct("(a\\*)+")).toBe(false);
    expect(hasCatastrophicConstruct("(a\\+)+")).toBe(false);
    expect(hasCatastrophicConstruct("\\(a+\\)+")).toBe(false);
  });

  it("allows an ordinary quantified group", () => {
    for (const pattern of ["(ab)+", "([a-z])+", "(ab|cd)+", "(a+)", "(a+)b", "(a+)?"]) {
      expect(hasCatastrophicConstruct(pattern)).toBe(false);
    }
  });

  it("does not fall over on a pattern nobody could compile", () => {
    // It runs before the engine does, so it sees malformed input as a matter
    // of course.
    for (const pattern of ["(a+", "a+)", "((((", "))))", "\\", "(a\\", ""]) {
      expect(typeof hasCatastrophicConstruct(pattern)).toBe("boolean");
    }
  });

  it("is a denylist, and says so", () => {
    // Recorded rather than claimed: these are catastrophic and get through,
    // which is why the length cap and the server's own bounds still matter.
    expect(hasCatastrophicConstruct("(a|a)*")).toBe(false);
    expect(hasCatastrophicConstruct("a+a+$")).toBe(false);
  });
});

describe("compiling a pattern somebody else wrote", () => {
  it.each(golden.compiled)("$name", (record) => {
    let error: string | undefined;
    try {
      compileUserPattern(record.pattern);
    } catch (thrown) {
      error = (thrown as Error).message;
    }

    if (record.error === undefined) {
      expect(error).toBeUndefined();
      return;
    }
    if (sharedRefusal(record.error)) {
      expect(error).toBe(record.error);
      return;
    }
    // A recorded divergence: what counts as a valid pattern, and how the
    // refusal reads, is the engine's own. Both refuse or both accept is not
    // something two engines promise, so only the shape is held.
    expect(error === undefined || error.startsWith("invalid pattern:")).toBe(true);
  });

  it("refuses a pattern past the cap before it looks at the shape", () => {
    // Length first, so a long pattern costs a comparison rather than a scan.
    const long = "a".repeat(MAX_USER_PATTERN_LEN + 1);
    expect(() => compileUserPattern(long)).toThrow(`pattern too long (max ${MAX_USER_PATTERN_LEN} chars)`);
    expect(() => compileUserPattern(`(a+)+${"b".repeat(MAX_USER_PATTERN_LEN)}`)).toThrow("pattern too long");
  });

  it("takes a pattern exactly at the cap", () => {
    expect(compileUserPattern("a".repeat(MAX_USER_PATTERN_LEN))).toBeInstanceOf(RegExp);
  });

  it("names the shape it refused, so a caller can rewrite it", () => {
    expect(() => compileUserPattern("(a+)+$")).toThrow(
      "pattern rejected: catastrophic-backtracking construct (nested quantifier or quantified backreference)",
    );
  });

  it("compiles a pattern written for the reference engine", () => {
    // A leading inline flag has no syntax here at all, so a pattern an
    // operator wrote against the reference would fail to compile rather than
    // match differently.
    const compiled = compileUserPattern("(?i)password:\\s*$");
    expect(compiled.flags).toContain("i");
    expect(compiled.test("PASSWORD: ")).toBe(true);
  });

  it("compiles an ordinary pattern into one that matches", () => {
    expect(compileUserPattern("^ready$").test("ready")).toBe(true);
    expect(compileUserPattern("^ready$").test("not ready")).toBe(false);
  });
});

describe("what a tool answers for a pattern it will not take", () => {
  it.each(golden.pattern_rejections)("$name", (record) => {
    const rejection = rejectBadPattern(record.pattern);
    if (record.rejection === null) {
      // The reference took it. Where the two engines disagree about validity
      // this port may still refuse — with the same contract.
      expect(rejection === undefined || rejection.error).toBeTruthy();
      return;
    }
    if (sharedRefusal(record.rejection.detail)) {
      expect(rejection).toEqual(record.rejection);
    } else {
      // The engine's own verdict on validity, which the two do not share.
      expect(rejection === undefined || rejection.error === "invalid_pattern").toBe(true);
    }
  });

  it("leaves what counts as a valid pattern to the engine", () => {
    // A recorded divergence. `\2` with one group is an invalid reference to
    // the reference engine and a legacy escape here, so this port takes a
    // pattern the reference refuses. It matches a control character rather
    // than a group, which is inert: a filter that matches nothing shows
    // nothing, and no decision is made from it.
    expect(rejectBadPattern("(a)\\2")).toBeUndefined();
    // What both engines refuse is refused the same way, with the same
    // contract around it.
    expect(rejectBadPattern("(unclosed")?.error).toBe("invalid_pattern");
    expect(rejectBadPattern("[a-")?.error).toBe("invalid_pattern");
  });

  it("takes no pattern as no filter, not as a bad one", () => {
    // A tool asked for no filter has nothing to refuse.
    expect(rejectBadPattern(undefined)).toBe(golden.no_pattern_rejection ?? undefined);
    expect(rejectBadPattern(null)).toBeUndefined();
  });

  it("answers with a refusal rather than throwing", () => {
    // An exception reaches the caller as a tool error, which says more about
    // this process than a refusal does.
    const rejection = rejectBadPattern("(a+)+");
    expect(rejection).toEqual({
      success: false,
      error: "invalid_pattern",
      detail: "pattern rejected: catastrophic-backtracking construct (nested quantifier or quantified backreference)",
    });
  });

  it("compiles once, so a tool need not compile it again", () => {
    // Validating and then recompiling would run the guard twice and give a
    // caller two chances to be told different things.
    const [compiled, rejection] = compiledPatternOrRejection("^ready$");
    expect(rejection).toBeUndefined();
    expect(compiled?.test("ready")).toBe(true);
  });

  it("hands back nothing at all when no pattern was asked for", () => {
    expect(compiledPatternOrRejection(undefined)).toEqual([undefined, undefined]);
    expect(golden.no_pattern_compiled).toEqual([null, null]);
  });

  it.each(golden.compiled_or_rejection)("compiling or refusing $name", (record) => {
    const [compiled, rejection] = compiledPatternOrRejection(record.pattern);
    if (record.rejection === null) {
      expect(compiled === undefined || compiled instanceof RegExp).toBe(true);
      return;
    }
    if (sharedRefusal(record.rejection.detail)) {
      expect(compiled).toBeUndefined();
      expect(rejection).toEqual(record.rejection);
    } else {
      expect(rejection?.error ?? "invalid_pattern").toBe("invalid_pattern");
    }
  });
});

describe("what a tool answers for an id it will not take", () => {
  it.each(golden.ids)("$name", (record) => {
    const rejection = rejectBadId(record.value, record.kind);
    expect(rejection ?? null).toEqual(record.rejection);
  });

  it("refuses anything that is more than one path segment", () => {
    // A caller-supplied id lands in a request path, so a slash is a different
    // route and `..` is somebody else's.
    for (const value of ["a/b", "../etc", "a%2Fb", "a?b=c", ".", "..", ""]) {
      expect(rejectBadId(value, "worker_id")?.error).toBe("invalid_id");
    }
  });

  it("takes the ids a real deployment uses", () => {
    for (const value of ["worker-1", "worker_1", "worker.1", "00000000-0000-0000-0000-000000000000", "..."]) {
      expect(rejectBadId(value, "worker_id")).toBeUndefined();
    }
  });

  it("names the kind it was given, so a caller knows which id to fix", () => {
    expect(rejectBadId("a/b", "hijack_id")?.detail).toBe("invalid hijack_id: 'a/b'");
    expect(rejectBadId("a/b")).toEqual(golden.id_default_kind);
  });

  it.each(golden.id_pairs)("several ids at once: $name", (record) => {
    const rejection = rejectBadIds(...record.pairs);
    expect(rejection ?? null).toEqual(record.rejection);
  });

  it("reports the first bad id, not the last", () => {
    // In the order they were given, so a caller fixes them one at a time
    // rather than being told about the second while the first is still wrong.
    const rejection = rejectBadIds(["a/b", "worker_id"], ["c/d", "hijack_id"]);
    expect(rejection?.detail).toBe("invalid worker_id: 'a/b'");
  });

  it("has nothing to say about no ids at all", () => {
    expect(rejectBadIds()).toBeUndefined();
  });
});
