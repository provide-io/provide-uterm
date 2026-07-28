//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Prompt detection with cursor-aware pattern matching.
 *
 * Port of the Python modules `provide.uterm.detection.detector` and
 * `provide.uterm.detection.detector_compile`.
 *
 * This decides whether a terminal is waiting for input. Wrong in one
 * direction and an agent types into a screen that is still drawing; wrong in
 * the other and it waits forever at a prompt it failed to recognise. Nearly
 * everything here exists to make one of those two less likely.
 */

import { createHash } from "node:crypto";
import { compilePySearch, pyEncodeReplace, pyReEscape } from "../pycompat/index.ts";

import type { ScreenSnapshot } from "./buffer.ts";

/**
 * What the detector will accept.
 *
 * A `Partial` of the shared snapshot, and deliberately so: every field is
 * treated as missing-able because a frame that arrived without one should
 * produce "no prompt" rather than an exception in the middle of a session.
 * The screen may also be null, which is what a producer with nothing to say
 * sends.
 */
export type DetectorSnapshot = Partial<Omit<ScreenSnapshot, "screen">> & {
  screen?: string | null;
  [key: string]: unknown;
};

/** A matched prompt pattern with its rule metadata. */
export interface PromptMatch {
  /** Which rule fired. */
  promptId: string;
  /** The whole rule, including keys this module never reads. */
  pattern: Record<string, unknown>;
  /** How the answer should be sent. */
  inputType: string;
  /** What terminates the answer. */
  eolPattern: string;
  /** Extraction instructions for whatever reads the reply. */
  kvExtract: unknown;
}

/** Why a pattern matched its regex but was not used. */
export interface PatternFailure {
  pattern_id: string;
  reason: string;
  [key: string]: unknown;
}

/** A detection result with its partial-match diagnostics. */
export interface PromptDetectionDiagnostics {
  match: PromptMatch | undefined;
  regexMatchedButFailed: PatternFailure[];
}

/** A pattern that could not be compiled. */
export interface CompileFailure {
  id: string;
  regex?: string;
  error: string;
}

/**
 * Raised in strict mode when a pattern fails to compile.
 *
 * The lenient default merely records failures and carries on with the
 * surviving patterns, which suits environments where a broken rule should not
 * take the whole detector offline. A deployment loading curated rules should
 * be strict, so a typo is caught at startup rather than silently detecting
 * less than it was written to.
 */
export class DetectorPatternCompileError extends Error {}

/** Options for {@link PromptDetector}. */
export interface PromptDetectorOptions {
  /** Collapses volatile prompt text before fingerprinting. */
  normalizer?: (regionText: string) => string;
  /** Refuse to start rather than skip a pattern that will not compile. */
  strict?: boolean;
}

/** How many lines from the bottom the fast pass searches. */
const DEFAULT_PROMPT_REGION_TAIL_LINES = 12;

/** A pattern and the expression it compiled to. */
type Compiled = [RegExp, Record<string, unknown>];

/** Read a rule's string field, or fall back. */
function stringField(pattern: Record<string, unknown>, key: string, fallback: string): string {
  const value = pattern[key];
  return typeof value === "string" ? value : fallback;
}

/** The rule's id, or the reference's placeholder for one that has none. */
function patternId(pattern: Record<string, unknown>): string {
  const id = pattern.id;
  return typeof id === "string" ? id : "unknown";
}

/** A whole number read out of an untrusted cursor, defaulting to the origin. */
function coordinate(value: unknown): number {
  // A malformed cursor must not raise mid-detection — a browser or a drifting
  // emulator can produce one, and losing detection over it would hang the
  // session.
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
}

/** Build a match from a rule, filling in what it left unsaid. */
function matchFromPattern(pattern: Record<string, unknown>): PromptMatch {
  return {
    promptId: pattern.id as string,
    // The whole rule travels, because extraction downstream reads keys this
    // module knows nothing about.
    pattern,
    inputType: stringField(pattern, "input_type", "multi_key"),
    eolPattern: stringField(pattern, "eol_pattern", "[\\r\\n]+"),
    kvExtract: pattern.kv_extract,
  };
}

/** Prompt detection over a set of operator-written patterns. */
export class PromptDetector {
  readonly #normalizer: ((regionText: string) => string) | undefined;
  readonly #strict: boolean;
  #patterns: Array<Record<string, unknown>>;
  #compileFailures: CompileFailure[] = [];
  #compiledAll: Compiled[] = [];
  /** The subset that never asks for the cursor — the fast pass when it is not there. */
  #compiledNoCursorEndRequired: Compiled[] = [];

  constructor(patterns: Array<Record<string, unknown>>, options: PromptDetectorOptions = {}) {
    this.#normalizer = options.normalizer;
    this.#strict = options.strict === true;
    this.#patterns = patterns;
    this.#recompile();
  }

  /** How many rules are loaded. */
  get patternCount(): number {
    return this.#patterns.length;
  }

  /**
   * The rules that would not compile.
   *
   * Always empty in strict mode, where the constructor would have raised
   * before returning.
   */
  get compileFailures(): readonly CompileFailure[] {
    return [...this.#compileFailures];
  }

  /**
   * Compile the current rule set.
   *
   * @throws {DetectorPatternCompileError} In strict mode, if any rule fails.
   */
  #compilePatterns(): Compiled[] {
    const compiled: Compiled[] = [];
    const failed: CompileFailure[] = [];

    for (const pattern of this.#patterns) {
      const regex = pattern.regex;
      if (typeof regex !== "string") {
        // A rule with no regex is a different fault from a rule with a broken
        // one, and an operator reading the log needs to tell them apart.
        failed.push({ id: patternId(pattern), error: "Missing key: 'regex'" });
        continue;
      }
      try {
        compiled.push([compilePySearch(regex), pattern]);
      } catch (error) {
        failed.push({ id: patternId(pattern), regex, error: String((error as Error).message) });
      }
    }

    if (failed.length > 0) {
      this.#compileFailures = failed;
      if (this.#strict) {
        const summary = failed.map((failure) => `${failure.id}: ${failure.error}`).join(", ");
        throw new DetectorPatternCompileError(
          `${failed.length} pattern(s) failed to compile in strict mode: ${summary}`,
        );
      }
    }
    return compiled;
  }

  /** Compile the current rule set and index it. */
  #recompile(): void {
    this.#compiledAll = this.#compilePatterns();
    this.#compiledNoCursorEndRequired = this.#compiledAll.filter(
      ([, pattern]) => pattern.expect_cursor_at_end === false,
    );
  }

  /**
   * Replace the rule set, rolling back if the new one will not compile.
   *
   * Without the rollback a failed reload would leave the detector holding a
   * poisoned list that raises on every call afterwards.
   */
  #swapPatterns(candidate: Array<Record<string, unknown>>): void {
    const saved = this.#patterns;
    this.#patterns = candidate;
    try {
      this.#recompile();
    } catch (error) {
      this.#patterns = saved;
      this.#recompile();
      throw error;
    }
  }

  /**
   * The slice of screen a prompt is most likely to be in.
   *
   * Anchored to the last line with content rather than to the bottom row,
   * because many UIs leave blank rows below it.
   *
   * @returns The region text, and whether the cursor is inside it.
   */
  static promptRegion(
    snapshot: DetectorSnapshot,
    tailLines: number = DEFAULT_PROMPT_REGION_TAIL_LINES,
  ): [string, boolean] {
    const screen = snapshot.screen ?? "";
    if (screen === "") {
      return ["", false];
    }

    const lines = screen.split("\n");
    let lastIndex = 0;
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      if ((lines[index] as string).trim() !== "") {
        lastIndex = index;
        break;
      }
    }
    // At least one line: a tail of zero would make the region empty and no
    // prompt would ever be found in the fast pass. The outer clamp changes
    // nothing here, since a negative start to `slice` is clamped to zero
    // anyway — it is the reference's, where a negative index counts from the
    // end and would return the wrong lines entirely.
    const startIndex = Math.max(0, lastIndex - Math.max(1, Math.trunc(tailLines)) + 1);

    const cursor: { x?: unknown; y?: unknown } = snapshot.cursor ?? {};
    const cursorY = coordinate(cursor.y);
    return [lines.slice(startIndex, lastIndex + 1).join("\n"), startIndex <= cursorY && cursorY <= lastIndex];
  }

  /** Collapse volatile prompt text before fingerprinting. */
  static normalizePromptRegion(regionText: string, normalizer?: (text: string) => string): string {
    if (regionText === "") {
      return "";
    }
    return normalizer === undefined ? regionText : normalizer(regionText);
  }

  /**
   * A cache key for a screen's detection result.
   *
   * The cursor state is part of it: two screens with identical text but a
   * moved cursor are different questions, and serving a stale answer to one
   * of them is how a session ends up typing at the wrong moment. The cost is
   * a cache miss whenever only the cursor moves.
   */
  promptFingerprint(snapshot: DetectorSnapshot, tailLines: number = DEFAULT_PROMPT_REGION_TAIL_LINES): string {
    const [region] = PromptDetector.promptRegion(snapshot, tailLines);
    const normalised = PromptDetector.normalizePromptRegion(region, this.#normalizer);
    // A lone surrogate becomes a replacement character rather than raising, so
    // an undecodable screen still fingerprints instead of losing detection.
    const digest = createHash("blake2s256").update(pyEncodeReplace(normalised)).digest("hex");
    const cursorAtEnd = Number((snapshot.cursor_at_end ?? true) ? 1 : 0);
    const trailing = Number(snapshot.has_trailing_space ? 1 : 0);
    const cursor: { x?: unknown; y?: unknown } = snapshot.cursor ?? {};
    return `${digest}:${cursorAtEnd}:${trailing}:${coordinate(cursor.x)}:${coordinate(cursor.y)}`;
  }

  /**
   * The exclusion regex a rule carries, in either of its two spellings.
   *
   * `negative_regex` is a plain expression; `negative_match` is a rule-shaped
   * object whose mode says how literally to take it.
   */
  static #resolveNegativeRegex(pattern: Record<string, unknown>): string | undefined {
    if (Object.hasOwn(pattern, "negative_regex")) {
      return String(pattern.negative_regex);
    }
    const negative = pattern.negative_match;
    // An empty object is falsy in the reference, so it is no exclusion at
    // all. The emptiness test is kept for that reason even though the
    // downstream "resolved to nothing" guard would catch it too: a rule that
    // supplied an empty object never asked for an exclusion, which is a
    // different thing from one whose exclusion came out empty.
    if (negative !== null && typeof negative === "object" && Object.keys(negative).length > 0) {
      const record = negative as Record<string, unknown>;
      const sub = String(record.pattern ?? "");
      const mode = String(record.match_mode);
      if (mode === "contains") {
        // Escaped, so a bracket in the text is a bracket rather than a
        // character class that would match something else entirely. CPython's
        // escaping, not the usual metacharacter set — the escaped text is
        // reported in the diagnostics below, and an operator debugging a rule
        // should read back what the reference would have shown them.
        return pyReEscape(sub);
      }
      if (mode === "exact") {
        return `^${pyReEscape(sub)}$`;
      }
      return sub;
    }
    return undefined;
  }

  /** Walk a compiled set over one piece of text. */
  #detectInText(
    text: string,
    fullScreen: string,
    cursorAtEnd: boolean,
    compiled: Compiled[],
    failures: PatternFailure[],
    cursorMissCandidates?: PromptMatch[],
  ): PromptMatch | undefined {
    for (const [regex, pattern] of compiled) {
      if (!regex.test(text)) {
        continue;
      }

      const negative = PromptDetector.#resolveNegativeRegex(pattern);
      // Exclusions are case-insensitive where positive patterns are not.
      // Deliberately asymmetric: a rule blocking "stardock" should block
      // "STARDOCK", while a prompt written for "Command:" must not fire on
      // "command:". The whole screen is searched, not the region, because an
      // exclusion is about the state of the session and may be written
      // anywhere on it.
      if (
        negative !== undefined &&
        negative !== "" &&
        compilePySearch(negative, { ignoreCase: true }).test(fullScreen)
      ) {
        failures.push({ pattern_id: pattern.id as string, reason: "negative_match", negative_pattern: negative });
        continue;
      }

      const expectCursorAtEnd = pattern.expect_cursor_at_end ?? true;
      if (expectCursorAtEnd && !cursorAtEnd) {
        failures.push({
          pattern_id: pattern.id as string,
          reason: "cursor_position",
          expected_cursor_at_end: expectCursorAtEnd,
          actual_cursor_at_end: cursorAtEnd,
        });
        // Kept aside rather than dropped. Cursor bookkeeping drifts on some
        // screens and some telnet bursts, and without this a session waits at
        // a prompt it can see forever.
        cursorMissCandidates?.push(matchFromPattern(pattern));
        continue;
      }

      return matchFromPattern(pattern);
    }
    return undefined;
  }

  /**
   * Search the likely region, then the whole screen.
   *
   * Region first is not only speed: a prompt still visible in scrollback would
   * otherwise fire ahead of the live one at the bottom.
   */
  #runTwoPassDetection(
    snapshot: DetectorSnapshot,
    screen: string,
    cursorAtEnd: boolean,
    compiledFast: Compiled[],
    compiledAll: Compiled[],
    failures: PatternFailure[],
  ): [PromptMatch | undefined, PromptMatch[]] {
    const cursorMissCandidates: PromptMatch[] = [];
    const [regionText, cursorInRegion] = PromptDetector.promptRegion(snapshot);
    if (regionText !== "") {
      // No candidate list: the fast set is either the no-cursor-required
      // subset or the full set under cursor-at-end, so the held-back branch
      // is unreachable here and threading a list that can never be written
      // would only suggest otherwise.
      const match = this.#detectInText(regionText, screen, cursorAtEnd, compiledFast, failures);
      if (match !== undefined) {
        return [match, cursorMissCandidates];
      }
    }

    if (!cursorInRegion) {
      const match = this.#detectInText(screen, screen, cursorAtEnd, compiledAll, failures, cursorMissCandidates);
      if (match !== undefined) {
        return [match, cursorMissCandidates];
      }
    }

    return [undefined, cursorMissCandidates];
  }

  /** Whether this screen is waiting for input, and for what. */
  detectPrompt(snapshot: DetectorSnapshot): PromptMatch | undefined {
    return this.detectPromptWithDiagnostics(snapshot).match;
  }

  /** The same decision, with the reasons a pattern matched but was not used. */
  detectPromptWithDiagnostics(snapshot: DetectorSnapshot): PromptDetectionDiagnostics {
    const screen = snapshot.screen ?? "";
    // Defaulting to true keeps detection working for the minimal snapshots
    // tests and older callers produce.
    const cursorAtEnd = snapshot.cursor_at_end ?? true;
    // Coerced at the boundary as the reference does. It is only ever read as
    // a gate below, so this changes no branch — it makes the value a bool for
    // anything that stores or logs it.
    const hasTrailingSpace = Boolean(snapshot.has_trailing_space);
    const failures: PatternFailure[] = [];

    const compiledFast = cursorAtEnd ? this.#compiledAll : this.#compiledNoCursorEndRequired;
    const [match, cursorMissCandidates] = this.#runTwoPassDetection(
      snapshot,
      screen,
      Boolean(cursorAtEnd),
      compiledFast,
      this.#compiledAll,
      failures,
    );
    if (match !== undefined) {
      return { match, regexMatchedButFailed: failures };
    }

    // A held-back match is used when a trailing space says the field is live.
    // A non-empty candidate list already implies the cursor check failed, so
    // there is nothing further to test for.
    if (cursorMissCandidates.length > 0 && hasTrailingSpace) {
      return { match: cursorMissCandidates[0] as PromptMatch, regexMatchedButFailed: failures };
    }

    return { match: undefined, regexMatchedButFailed: failures };
  }

  /** Add a rule to the set. */
  addPattern(pattern: Record<string, unknown>): void {
    this.#swapPatterns([...this.#patterns, pattern]);
  }

  /** Replace the whole rule set. */
  reloadPatterns(patterns: Array<Record<string, unknown>>): void {
    // Copied, so the caller's array changing later does not change the
    // detector underneath them.
    this.#swapPatterns([...patterns]);
  }
}
