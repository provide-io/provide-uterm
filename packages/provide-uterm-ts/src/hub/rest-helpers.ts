//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Prompt guards for the hijack REST contract.
 *
 * Port of the Python module `provide.uterm.server.bridge.rest_helpers` and
 * the Go package `hub` (`resthelpers.go`).
 *
 * A REST caller attaches these to a keystroke send: "type this, then wait
 * until the screen looks like *that*". Both failure directions cost the
 * caller — a guard that matches too eagerly returns before the command has
 * run, and one that never matches hangs until the timeout.
 */

import { validatePatternSafety } from "./pattern-safety.ts";

/**
 * Longest guard pattern a caller may supply.
 *
 * The bound is about work, not storage: this is searched against a whole
 * screen on every poll.
 */
export const MAX_EXPECT_REGEX_LEN = 200;

/** Why a guard pattern was refused. */
export type PromptRegexErrorKind = "too_long" | "unsafe" | "invalid";

/** Raised when a prompt guard pattern is refused. */
export class PromptRegexError extends Error {
  /** Which rule refused it; callers surface this. */
  readonly kind: PromptRegexErrorKind;
  /** The limit in force, when the refusal was about length. */
  readonly maxLength: number | undefined;

  constructor(message: string, kind: PromptRegexErrorKind, maxLength?: number) {
    super(message);
    this.name = "PromptRegexError";
    this.kind = kind;
    this.maxLength = maxLength;
  }
}

/** Options for {@link compileExpectRegex}. */
export interface CompileExpectRegexOptions {
  /** Overrides {@link MAX_EXPECT_REGEX_LEN}. */
  maxLength?: number;
}

/** Options for {@link snapshotMatches}. */
export interface SnapshotGuards {
  /** Require the detected prompt to have this id. */
  expectPromptId?: string | undefined;
  /** Require the screen to match this pattern. */
  expectRegex?: RegExp | undefined;
}

/**
 * The detected prompt id in a snapshot, if there is one.
 *
 * Every shape this declines is one a worker can actually send, so it returns
 * nothing rather than throwing on any of them. An empty id is declined too:
 * it would compare equal to an empty guard and match everything.
 */
export function extractPromptId(snapshot?: Record<string, unknown>): string | undefined {
  if (snapshot === undefined) {
    return undefined;
  }
  const detected = snapshot.prompt_detected;
  // The array check mirrors the reference's isinstance(dict); JSON cannot
  // produce an array carrying a prompt_id, so it is faithful rather than
  // load-bearing.
  if (typeof detected !== "object" || detected === null || Array.isArray(detected)) {
    return undefined;
  }
  const value = (detected as Record<string, unknown>).prompt_id;
  return typeof value === "string" && value !== "" ? value : undefined;
}

/**
 * Compile a prompt guard pattern.
 *
 * Refuses in three ways, each with its own kind. The length check runs
 * *first*: an over-long pattern must never reach the safety validator, or a
 * caller could spend the validator's time on a megabyte of input.
 *
 * Compiled case-insensitive and multiline, matching the poll loop's flags —
 * prompt text varies in case, and a guard describes one line of a screen
 * rather than the whole buffer. The `g` flag is deliberately *absent*: a
 * global pattern carries `lastIndex` between calls and would report a miss on
 * the next poll of an unchanged screen.
 *
 * @throws {PromptRegexError} When the pattern is too long, unsafe, or will
 *   not compile.
 */
export function compileExpectRegex(pattern?: string, options: CompileExpectRegexOptions = {}): RegExp | undefined {
  if (pattern === undefined || pattern === "") {
    return undefined;
  }
  const maxLength = options.maxLength ?? MAX_EXPECT_REGEX_LEN;
  if (pattern.length > maxLength) {
    throw new PromptRegexError("expect_regex too long", "too_long", maxLength);
  }
  try {
    validatePatternSafety(pattern);
  } catch (error) {
    throw new PromptRegexError(`unsafe expect_regex: ${(error as Error).message}`, "unsafe", maxLength);
  }
  try {
    return new RegExp(pattern, "im");
  } catch (error) {
    throw new PromptRegexError(`invalid expect_regex: ${(error as Error).message}`, "invalid", maxLength);
  }
}

/**
 * Whether `snapshot` satisfies the given guards.
 *
 * An absent snapshot never matches, even with no guards at all: "nothing to
 * check" is not "everything passed" — the caller is waiting for the worker to
 * say something, not for it to stay silent.
 *
 * An empty prompt id is no constraint rather than a constraint on emptiness.
 * When both guards are given, both must hold.
 */
export function snapshotMatches(snapshot: Record<string, unknown> | undefined, guards: SnapshotGuards): boolean {
  if (snapshot === undefined) {
    return false;
  }
  const { expectPromptId, expectRegex } = guards;
  if (expectPromptId !== undefined && expectPromptId !== "" && extractPromptId(snapshot) !== expectPromptId) {
    return false;
  }
  if (expectRegex === undefined) {
    return true;
  }
  return expectRegex.test(String(snapshot.screen ?? ""));
}
