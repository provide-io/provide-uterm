//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { DetectorPatternCompileError, type DetectorSnapshot, PromptDetector } from "./index.ts";

interface RecordedMatch {
  prompt_id: string;
  pattern: Record<string, unknown>;
  input_type: string;
  eol_pattern: string;
  kv_extract: unknown;
}

interface DetectorGolden {
  patterns: Array<Record<string, unknown>>;
  screen: string;
  detect: Array<{
    name: string;
    snapshot: DetectorSnapshot;
    match: RecordedMatch | null;
    failures: Array<Record<string, unknown>>;
  }>;
  negative: Array<{
    name: string;
    patterns: Array<Record<string, unknown>>;
    screen: string;
    matched: boolean;
    prompt_id: string | null;
    failures: Array<Record<string, unknown>>;
  }>;
  region: Array<{
    name: string;
    screen: string;
    tail_lines: number;
    cursor_y: number;
    region: string;
    cursor_in_region: boolean;
  }>;
  fingerprints: Array<{ name: string; snapshot: DetectorSnapshot; fingerprint: string }>;
  surrogate_fingerprint: string;
  tall: string;
  rich_pattern: RecordedMatch;
  python_non_string_input_type_error: string;
  compile: {
    lenient_pattern_count: number;
    lenient_failures: Array<{ id: string; regex?: string; error: string }>;
    lenient_still_detects: RecordedMatch | null;
    strict_error: string;
    reload_error: string;
    survivor_pattern_count: number;
    survivor_still_detects: RecordedMatch | null;
    grown_count: number;
    grown_detects_new: RecordedMatch | null;
    grown_detects_old: RecordedMatch | null;
    replaced_count: number;
    replaced_detects_new: RecordedMatch | null;
    replaced_detects_old: RecordedMatch | null;
    ordered_detects: RecordedMatch | null;
    twice_failures: Array<{ id: string; error: string }>;
    python_non_string_regex_error: string;
  };
  defaults: { input_type: string; eol_pattern: string; kv_extract: unknown };
}

const golden = loadGolden<DetectorGolden>("detector_golden.json");

/** A match in the shape the corpus recorded it. */
function wire(match: ReturnType<PromptDetector["detectPrompt"]>): RecordedMatch | null {
  return match === undefined
    ? null
    : {
        prompt_id: match.promptId,
        pattern: match.pattern,
        input_type: match.inputType,
        eol_pattern: match.eolPattern,
        kv_extract: match.kvExtract ?? null,
      };
}

/** A detector over the corpus's own pattern set. */
function detector(): PromptDetector {
  return new PromptDetector(golden.patterns);
}

/** The recorded detect case with this name. */
function detectCase(name: string) {
  return golden.detect.find((entry) => entry.name === name);
}

describe("deciding whether a screen is waiting for input", () => {
  it.each(golden.detect)("$name", (record) => {
    const result = detector().detectPromptWithDiagnostics(record.snapshot);
    expect(wire(result.match)).toStrictEqual(record.match);
    // The partial-match reasons are compared too. They are what an operator
    // reads when a rule is not firing, and a pass that searched the wrong
    // text or the wrong pattern set reaches the same verdict by a different
    // route — which shows up here and nowhere else.
    expect(result.regexMatchedButFailed).toStrictEqual(record.failures);
  });

  it("reports the same match through the diagnostic call", () => {
    // The plain call is documented as the diagnostic one's match, so the two
    // must not be able to disagree.
    for (const record of golden.detect) {
      const subject = detector();
      expect(wire(subject.detectPromptWithDiagnostics(record.snapshot).match)).toStrictEqual(
        wire(subject.detectPrompt(record.snapshot)),
      );
    }
  });

  it("says nothing about a screen with nothing on it", () => {
    // A missing, null or empty screen is not a prompt. Typing into one is
    // typing into a session that has not started.
    for (const name of ["an empty screen", "a missing screen", "a null screen"]) {
      expect(detectCase(name)?.match).toBeNull();
    }
  });

  it("finds a prompt below blank rows", () => {
    // Many UIs leave the bottom rows empty, so anchoring to the bottom row
    // rather than the last line with content would find nothing.
    expect(detectCase("a prompt below blank rows")?.match?.prompt_id).toBe("command");
  });

  it("finds a prompt that has scrolled out of the region", () => {
    // The second pass covers the whole screen, so a prompt further up is
    // still found rather than the session hanging.
    expect(detectCase("a prompt only in scrollback")?.match?.prompt_id).toBe("command");
  });

  it("prefers a live prompt over a stale one further up", () => {
    // The region pass runs first for exactly this: a prompt still visible in
    // scrollback would otherwise answer ahead of the one at the bottom.
    expect(detectCase("a stale prompt above a live one")?.match?.prompt_id).toBe("command");
  });

  it("searches the region, not the whole screen, on the first pass", () => {
    // With the stale prompt's rule listed *first*, a pass that searched the
    // whole screen would answer with it — rule order would beat position.
    // Only searching the region keeps the live prompt at the bottom winning.
    const subject = new PromptDetector([
      { id: "stale", regex: "Enter your name:" },
      { id: "live", regex: "Command \\[" },
    ]);
    const screen = `Enter your name:\n${Array.from({ length: 30 }, (_, n) => `filler ${n}`).join("\n")}\nCommand [TL=00:00:00]:? `;
    expect(subject.detectPrompt({ screen, screen_hash: "h" })?.promptId).toBe("live");
  });

  it("ignores a line that is only whitespace when finding the bottom", () => {
    // Trailing spaces are still blank as far as a reader is concerned, and
    // anchoring to one would put the region below the prompt.
    expect(detectCase("a whitespace-only last line")?.match?.prompt_id).toBe("command");
  });

  it("reads a fractional cursor as a whole line", () => {
    expect(detectCase("a fractional cursor")?.match?.prompt_id).toBe("command");
  });

  it("takes the first pattern that matches, in the order given", () => {
    // Rule order is the author's priority. Two prompts on one screen resolve
    // to whichever pattern was listed first.
    const record = detectCase("two prompts, the first pattern wins");
    expect(record?.match?.prompt_id).toBe("command");
    expect(golden.patterns.findIndex((pattern) => pattern.id === "command")).toBeLessThan(
      golden.patterns.findIndex((pattern) => pattern.id === "name"),
    );
  });
});

describe("the cursor test", () => {
  it("holds back a pattern that wanted the cursor at the end", () => {
    // Typing into a screen that is still drawing is the failure this avoids.
    expect(detectCase("the cursor is not at the end")?.match).toBeNull();
    // Recorded as a partial match only on the full-screen pass — the fast
    // pass drops to the patterns that never asked for the cursor, so there is
    // nothing for it to reject.
    expect(detectCase("the same, without the trailing space")?.failures[0]?.reason).toBe("cursor_position");
  });

  it("lets a pattern through that never asked for it", () => {
    // "press any key" is true wherever the cursor is.
    expect(detectCase("a pattern that does not need the cursor")?.match?.prompt_id).toBe("any_key");
  });

  it("uses a held-back match when a trailing space says the field is live", () => {
    // The cursor bookkeeping drifts on some screens and some telnet bursts.
    // Without this fallback such a session waits at a prompt forever.
    expect(detectCase("the cursor is above the region and wrong, with a trailing space")?.match?.prompt_id).toBe(
      "command",
    );
  });

  it("takes a trailing flag that is not a boolean at its word", () => {
    // Coerced, so a frontend sending "yes" gets the fallback rather than
    // silently not getting it.
    expect(detectCase("a trailing flag that is not a boolean")?.match?.prompt_id).toBe("command");
  });

  it("uses the first held-back candidate, not the last", () => {
    // Rule order is the author's priority, and it still applies to the
    // fallback — otherwise the least-specific rule answers.
    const subject = new PromptDetector([
      { id: "first", regex: "Command" },
      { id: "second", regex: "Command \\[" },
    ]);
    const match = subject.detectPrompt({
      screen: golden.tall,
      screen_hash: "h",
      cursor: { x: 0, y: 0 },
      cursor_at_end: false,
      has_trailing_space: true,
    });
    expect(match?.promptId).toBe("first");
  });

  it("does not use one without that evidence", () => {
    expect(detectCase("the same, without the trailing space")?.match).toBeNull();
  });

  it("only reaches the fallback through the full-screen pass", () => {
    // The region pass never records a held-back candidate, so a cursor inside
    // the region means there is nothing to fall back to — which is why the
    // two cases above put the cursor above it.
    expect(detectCase("the cursor is wrong but there is a trailing space")?.match).toBeNull();
  });
});

describe("exclusions", () => {
  it.each(golden.negative)("$name", (record) => {
    const subject = new PromptDetector(record.patterns);
    const result = subject.detectPromptWithDiagnostics({ screen: record.screen, screen_hash: "h" });
    expect(result.match !== undefined).toBe(record.matched);
    expect(result.match?.promptId ?? null).toBe(record.prompt_id);
    expect(result.regexMatchedButFailed).toStrictEqual(record.failures);
  });

  /** The recorded exclusion case with this name. */
  function negativeCase(name: string) {
    return golden.negative.find((entry) => entry.name === name);
  }

  it("blocks a match when the exclusion is on screen", () => {
    expect(negativeCase("a negative regex blocks it")?.matched).toBe(false);
    expect(negativeCase("the negative regex is not on screen")?.matched).toBe(true);
  });

  it("ignores case in an exclusion but not in a prompt", () => {
    // Deliberately asymmetric: exclusions are broad guards, prompts are
    // precise, and authors rely on exact case to tell prompts apart.
    expect(negativeCase("a negative regex is case insensitive")?.matched).toBe(false);
    expect(negativeCase("the positive pattern is not")?.matched).toBe(false);
  });

  it("takes a contains-mode match literally", () => {
    // Escaped, so a bracket in the text is a bracket and not a character
    // class that matches something else entirely.
    expect(negativeCase("a contains-mode match with regex characters in it")?.matched).toBe(false);
  });

  it("anchors an exact-mode match to a whole line", () => {
    expect(negativeCase("an exact-mode negative match")?.matched).toBe(false);
    expect(negativeCase("an exact-mode match that is only a substring")?.matched).toBe(true);
  });

  it("treats a mode-less negative match as a regex", () => {
    expect(negativeCase("a negative match with no mode is a regex")?.matched).toBe(false);
  });

  it("ignores an unusable negative match rather than blocking everything", () => {
    // A malformed exclusion that silently blocked every prompt would look
    // exactly like a hung session.
    for (const name of [
      "an empty negative match dict",
      "a negative match that is not a dict",
      "a null negative match",
    ]) {
      expect(negativeCase(name)?.matched).toBe(true);
    }
  });

  it("reads negative_regex by presence, not by value", () => {
    // The reference asks whether the key is there. A rule that carries the
    // key with nothing in it has said something — that it wants no
    // exclusion from the other spelling — and falling through to
    // negative_match would apply one it deliberately overrode.
    const subject = new PromptDetector([
      {
        id: "cmd",
        regex: "Command",
        negative_regex: undefined,
        negative_match: { pattern: "STARDOCK", match_mode: "contains" },
      },
    ]);
    expect(subject.detectPrompt({ screen: golden.screen, screen_hash: "h" })?.promptId).toBe("cmd");
  });

  it("prefers negative_regex when a pattern carries both", () => {
    expect(negativeCase("negative_regex wins over negative_match")?.matched).toBe(true);
  });

  it("treats an exclusion that resolves to nothing as no exclusion", () => {
    // An empty pattern matches everywhere. Used as an exclusion it would
    // block every prompt, which looks exactly like a hung session.
    expect(negativeCase("an empty negative regex blocks nothing")?.matched).toBe(true);
    expect(negativeCase("an empty pattern in a contains-mode match")?.matched).toBe(true);
  });

  it("searches the whole screen, not the region", () => {
    // An exclusion is about the state of the session, which may be written
    // anywhere on it — the region would miss a banner at the top.
    expect(negativeCase("the exclusion only looks at the whole screen")?.matched).toBe(false);
  });
});

describe("the region a prompt is looked for in", () => {
  it.each(golden.region)("$name", (record) => {
    const snapshot: DetectorSnapshot = {
      screen: record.screen,
      screen_hash: "h",
      cursor: { x: 0, y: record.cursor_y },
    };
    expect(PromptDetector.promptRegion(snapshot, record.tail_lines)).toStrictEqual([
      record.region,
      record.cursor_in_region,
    ]);
  });

  it("ends at the last line with content", () => {
    const record = golden.region.find((entry) => entry.name.startsWith("trailing blank rows are ignored"));
    expect(record?.region.endsWith("two")).toBe(true);
  });

  it("treats a zero or negative tail as one line", () => {
    // A tail of zero would be an empty region and no prompt would ever be
    // found in the fast pass.
    for (const prefix of ["zero lines is treated as one", "a negative tail is treated as one"]) {
      const record = golden.region.find((entry) => entry.name.startsWith(prefix));
      expect(record?.region.includes("\n")).toBe(false);
    }
  });

  it("copes with a tail longer than the screen", () => {
    const record = golden.region.find((entry) => entry.name.startsWith("more lines than there are"));
    expect(record?.region).toBe(golden.screen.replace(/\n$/, ""));
  });

  it("defaults the tail when none is given", () => {
    // Checked against a screen taller than the tail, so a different default
    // would return a different region rather than the same short one.
    const snapshot: DetectorSnapshot = { screen: golden.tall, screen_hash: "h", cursor: { x: 0, y: 0 } };
    const expected = golden.region.find((entry) => entry.name === "a tall screen at the default tail (cursor y=0)");
    expect(PromptDetector.promptRegion(snapshot)).toStrictEqual([expected?.region, expected?.cursor_in_region]);
    expect(expected?.region.split("\n")).toHaveLength(12);
  });

  it("ignores whitespace-only lines when finding the bottom", () => {
    const record = golden.region.find((entry) => entry.name.startsWith("a whitespace-only last line"));
    expect(record?.region).toBe("content");
  });
});

describe("the fingerprint", () => {
  it.each(golden.fingerprints)("$name", (record) => {
    expect(detector().promptFingerprint(record.snapshot)).toBe(record.fingerprint);
  });

  it("is the same for the same screen", () => {
    // It is a cache key; an unstable one would re-run detection on every
    // frame and defeat the point of having it.
    const first = golden.fingerprints.find((entry) => entry.name === "a plain screen");
    const again = golden.fingerprints.find((entry) => entry.name === "the same screen again");
    expect(again?.fingerprint).toBe(first?.fingerprint);
  });

  it("covers the region, not the whole screen", () => {
    // Two screens with the same tail and different text above it are the same
    // question, so they share a cache entry rather than each missing.
    const tall = golden.fingerprints.find((entry) => entry.name === "a tall screen");
    const other = golden.fingerprints.find((entry) => entry.name === "the same tail, a different banner");
    expect(other?.fingerprint).toBe(tall?.fingerprint);
  });

  it("reads a fractional cursor as a whole line", () => {
    const fractional = golden.fingerprints.find((entry) => entry.name === "a fractional cursor");
    expect(fractional?.fingerprint.endsWith(":1:2")).toBe(true);
  });

  it("changes when the screen does", () => {
    const first = golden.fingerprints.find((entry) => entry.name === "a plain screen");
    const other = golden.fingerprints.find((entry) => entry.name === "a different screen");
    expect(other?.fingerprint).not.toBe(first?.fingerprint);
  });

  it("changes when only the cursor moves", () => {
    // Two screens with identical text but a moved cursor are different
    // questions, so a stale answer must not be served for one of them.
    const names = [
      "a plain screen",
      "the cursor moved",
      "the cursor moved again",
      "the cursor is not at the end",
      "there is a trailing space",
    ];
    const prints = names.map((name) => golden.fingerprints.find((entry) => entry.name === name)?.fingerprint);
    expect(new Set(prints).size).toBe(names.length);
  });

  it("copes with a cursor it cannot read", () => {
    // A malformed cursor must not raise mid-detection; it reads as the origin.
    for (const name of ["a cursor that is not a number", "a null cursor", "a cursor with no coordinates"]) {
      expect(golden.fingerprints.find((entry) => entry.name === name)?.fingerprint).toBeTruthy();
    }
  });

  it("copes with a screen that is not valid text", () => {
    // A lone surrogate is replaced rather than raising, so an undecodable
    // screen still fingerprints.
    expect(detector().promptFingerprint({ screen: "bad \ud800 char", screen_hash: "h" })).toBe(
      golden.surrogate_fingerprint,
    );
  });

  it("runs the caller's normaliser over the region", () => {
    // Volatile fields — a clock in the prompt — would otherwise make every
    // frame a cache miss.
    const plain = new PromptDetector(golden.patterns);
    const normalised = new PromptDetector(golden.patterns, { normalizer: () => "constant" });
    expect(normalised.promptFingerprint({ screen: "one", screen_hash: "h" })).toBe(
      normalised.promptFingerprint({ screen: "two", screen_hash: "h" }),
    );
    expect(plain.promptFingerprint({ screen: "one", screen_hash: "h" })).not.toBe(
      plain.promptFingerprint({ screen: "two", screen_hash: "h" }),
    );
  });

  it("leaves an empty region alone rather than normalising it", () => {
    expect(PromptDetector.normalizePromptRegion("", () => "called")).toBe("");
    expect(PromptDetector.normalizePromptRegion("text")).toBe("text");
    expect(PromptDetector.normalizePromptRegion("text", (value) => value.toUpperCase())).toBe("TEXT");
  });
});

describe("a rule set that does not compile", () => {
  const broken = [
    { id: "good", regex: "Command" },
    { id: "bad", regex: "unclosed (" },
    { id: "missing", input_type: "line" },
    { regex: "[unclosed" },
  ];

  it("keeps the patterns that did compile", () => {
    // One typo in a rules file must not take detection offline.
    const subject = new PromptDetector(broken);
    expect(subject.patternCount).toBe(golden.compile.lenient_pattern_count);
    expect(wire(subject.detectPrompt({ screen: golden.screen, screen_hash: "h" }))).toStrictEqual(
      golden.compile.lenient_still_detects,
    );
  });

  it("records what failed and why", () => {
    // Silently reduced coverage is the thing an operator cannot see.
    const failures = new PromptDetector(broken).compileFailures;
    expect(failures.map((failure) => failure.id)).toStrictEqual(
      golden.compile.lenient_failures.map((failure) => failure.id),
    );
    expect(failures).toHaveLength(3);
  });

  it("replaces the recorded failures rather than accumulating them", () => {
    // A rule set that fails twice would otherwise report the first failure
    // for ever, pointing an operator at a rule they have already removed.
    const subject = new PromptDetector([{ id: "bad", regex: "(" }]);
    subject.reloadPatterns([{ id: "bad2", regex: "[" }]);
    expect(subject.compileFailures.map((failure) => failure.id)).toStrictEqual(
      golden.compile.twice_failures.map((failure) => failure.id),
    );
  });

  it("records a regex that is not a string as a failure", () => {
    // A deliberate divergence. The reference does not survive this at all —
    // re.compile raises TypeError, which compile_patterns does not catch, so
    // the detector fails to construct even in lenient mode, whose whole
    // purpose is to keep going. Go coerces it to an empty pattern, which
    // matches every screen and turns one typo into a rule that always fires.
    // Treating it as a compile failure is the only reading that does what
    // lenient mode says it does.
    expect(golden.compile.python_non_string_regex_error).toContain("must be string");
    const subject = new PromptDetector([
      { id: "numeric", regex: 123 },
      { id: "good", regex: "Command" },
    ]);
    expect(subject.compileFailures.map((failure) => failure.id)).toStrictEqual(["numeric"]);
    expect(subject.detectPrompt({ screen: golden.screen, screen_hash: "h" })?.promptId).toBe("good");
  });

  it("names a pattern with no id as unknown rather than dropping it", () => {
    const failures = new PromptDetector(broken).compileFailures;
    expect(failures[2]?.id).toBe("unknown");
  });

  it("distinguishes a bad regex from a missing one", () => {
    const failures = new PromptDetector(broken).compileFailures;
    expect(failures[1]?.error).toContain("Missing key");
    expect(failures[0]?.error).not.toContain("Missing key");
  });

  it("raises in strict mode instead", () => {
    // A curated production rule set should fail at startup rather than
    // quietly detecting less than it was written to.
    expect(() => new PromptDetector(broken, { strict: true })).toThrow(DetectorPatternCompileError);
    expect(() => new PromptDetector(broken, { strict: true })).toThrow(/3 pattern\(s\) failed to compile/);
  });

  it("is lenient unless asked otherwise", () => {
    expect(() => new PromptDetector(broken)).not.toThrow();
    expect(() => new PromptDetector(broken, { strict: false })).not.toThrow();
  });

  it("has nothing to report when strict mode succeeds", () => {
    expect(new PromptDetector([{ id: "good", regex: "Command" }], { strict: true }).compileFailures).toStrictEqual([]);
  });
});

describe("changing the rules while running", () => {
  it("adds a pattern without losing the others", () => {
    const subject = new PromptDetector([{ id: "good", regex: "Command" }]);
    subject.addPattern({ id: "second", regex: "Enter" });
    expect(subject.patternCount).toBe(golden.compile.grown_count);
    expect(wire(subject.detectPrompt({ screen: "Enter your name: ", screen_hash: "h" }))).toStrictEqual(
      golden.compile.grown_detects_new,
    );
    expect(wire(subject.detectPrompt({ screen: golden.screen, screen_hash: "h" }))).toStrictEqual(
      golden.compile.grown_detects_old,
    );
  });

  it("appends rather than prepending", () => {
    // Rule order is the author's priority, so an added rule goes last and
    // cannot quietly take precedence over one already loaded.
    const subject = new PromptDetector([{ id: "first", regex: "Command" }]);
    subject.addPattern({ id: "appended", regex: "Command \\[" });
    expect(wire(subject.detectPrompt({ screen: golden.screen, screen_hash: "h" }))).toStrictEqual(
      golden.compile.ordered_detects,
    );
    expect(golden.compile.ordered_detects?.prompt_id).toBe("first");
  });

  it("replaces the whole set on a reload", () => {
    const subject = new PromptDetector([{ id: "good", regex: "Command" }]);
    subject.reloadPatterns([{ id: "other", regex: "Enter" }]);
    expect(subject.patternCount).toBe(golden.compile.replaced_count);
    expect(wire(subject.detectPrompt({ screen: "Enter your name: ", screen_hash: "h" }))).toStrictEqual(
      golden.compile.replaced_detects_new,
    );
    expect(wire(subject.detectPrompt({ screen: golden.screen, screen_hash: "h" }))).toStrictEqual(
      golden.compile.replaced_detects_old,
    );
  });

  it("keeps the old rules when a reload does not compile", () => {
    // Rolled back before raising, so a bad reload does not leave a detector
    // holding a poisoned set that raises on every call afterwards.
    const subject = new PromptDetector([{ id: "good", regex: "Command" }], { strict: true });
    expect(() => subject.reloadPatterns([{ id: "bad", regex: "(" }])).toThrow(DetectorPatternCompileError);
    expect(subject.patternCount).toBe(golden.compile.survivor_pattern_count);
    expect(wire(subject.detectPrompt({ screen: golden.screen, screen_hash: "h" }))).toStrictEqual(
      golden.compile.survivor_still_detects,
    );
  });

  it("does not hold on to the caller's list", () => {
    // A reload that aliased the caller's array would change underneath the
    // detector the next time they touched it.
    const patterns = [{ id: "good", regex: "Command" }];
    const subject = new PromptDetector([{ id: "first", regex: "x" }]);
    subject.reloadPatterns(patterns);
    patterns.push({ id: "sneaked", regex: "Enter" });
    expect(subject.patternCount).toBe(1);
  });
});

describe("what a pattern leaves unsaid", () => {
  it("fills in the defaults the reference does", () => {
    // A rule that names only a regex still has to produce a usable match:
    // multi_key input, and an end-of-line pattern to send after it.
    const subject = new PromptDetector([{ id: "d", regex: "x" }]);
    const match = subject.detectPrompt({ screen: "x", screen_hash: "h" });
    expect(match?.inputType).toBe(golden.defaults.input_type);
    expect(match?.eolPattern).toBe(golden.defaults.eol_pattern);
    expect(match?.kvExtract ?? null).toBe(golden.defaults.kv_extract);
  });

  it("carries extraction instructions through untouched", () => {
    const subject = new PromptDetector([{ id: "rich", regex: "x", kv_extract: [{ key: "a", regex: "(.*)" }] }]);
    expect(wire(subject.detectPrompt({ screen: "x", screen_hash: "h" }))).toStrictEqual(golden.rich_pattern);
  });

  it("falls back rather than raising on a field of the wrong type", () => {
    // Another deliberate divergence, on input the reference rejects outright:
    // its model refuses a non-string input_type with a ValidationError. A
    // detector that raised mid-frame would take the session down over a
    // cosmetic field, so this defaults instead.
    expect(golden.python_non_string_input_type_error).toBe("ValidationError");
    const subject = new PromptDetector([{ id: "d", regex: "x", input_type: 7, eol_pattern: 9 }]);
    const match = subject.detectPrompt({ screen: "x", screen_hash: "h" });
    expect(match?.inputType).toBe(golden.defaults.input_type);
    expect(match?.eolPattern).toBe(golden.defaults.eol_pattern);
  });

  it("carries the whole rule through, not just the fields it read", () => {
    // Downstream extraction reads keys this does not know about.
    const subject = new PromptDetector([{ id: "d", regex: "x", custom: "kept" }]);
    expect(subject.detectPrompt({ screen: "x", screen_hash: "h" })?.pattern.custom).toBe("kept");
  });
});
