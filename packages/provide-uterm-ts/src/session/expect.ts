//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Guarded sends, across any transport.
 *
 * Port of the Python module `provide.uterm.expect`.
 *
 * The shape an automation actually wants: type this, then wait until the
 * screen says that. The waiting is driven by the session's own change
 * notifications rather than by polling, so a fast reply costs one round trip
 * and a stalled session is noticed rather than waited out.
 */

import { compilePyPattern } from "../pycompat/index.ts";
import { prepareKeystrokes } from "../sanitizer/index.ts";

/** Default wait, in milliseconds. */
const DEFAULT_TIMEOUT_MS = 5000;

/** The session surface a guarded send needs. */
export interface ExpectSession {
  /** Write keystrokes to the far end. */
  send(data: string): Promise<void>;
  /** The current screen state. */
  snapshot(): Record<string, unknown>;
  /** A counter that advances whenever the screen changes. */
  screenChangeSeq(): number;
  /** Resolve when the screen changes, or on timeout. Reports which. */
  waitForScreenChange(options: { timeoutMs: number; since?: number }): Promise<boolean>;
}

/** The outcome of a guarded send. */
export interface ExpectResult {
  /** Whether a guard was satisfied. */
  matched: boolean;
  /** What satisfied it — the literal, or the whole regex match. */
  matchedText?: string | undefined;
  /** The screen as last read. */
  screen: string;
  /** Whether the wait ran out or the session went quiet. */
  timedOut: boolean;
}

/** Options for {@link sendAndExpect}. */
export interface SendAndExpectOptions {
  /** Wait until the screen contains this literal. */
  expectText?: string;
  /** Wait until the screen matches this pattern. */
  expectRegex?: string;
  /** How long to wait. */
  timeoutMs?: number;
  /** Whether to sanitise the keystrokes first. Defaults to true. */
  sanitize?: boolean;
}

/** The screen from a snapshot, or empty when it has none. */
function screenOf(session: ExpectSession): string {
  return String(session.snapshot()["screen"] ?? "");
}

/**
 * What satisfied the guards, if anything did.
 *
 * The literal is checked first and is what comes back — the caller gets the
 * thing they asked for rather than whatever the pattern happened to capture,
 * which is what makes the result useful for reporting *why* the wait ended.
 * A pattern returns its whole match rather than a group.
 *
 * An empty guard matches, and the match is the empty string. Callers must
 * test for presence rather than truthiness or they will read that as a miss.
 */
export function findMatch(screen: string, expectText?: string, expectRegex?: string): string | undefined {
  if (expectText !== undefined && screen.includes(expectText)) {
    return expectText;
  }
  if (expectRegex !== undefined) {
    const pattern = compilePyPattern(expectRegex);
    pattern.lastIndex = 0;
    const found = pattern.exec(screen);
    if (found !== null) {
      return found[0];
    }
  }
  return undefined;
}

/**
 * Send keys and wait until the expected text or pattern appears.
 *
 * An empty payload is not sent at all, so the same call doubles as a pure
 * read or settle without emitting a frame the far end would take as a
 * keystroke.
 *
 * With no guard the call means "send this and let the screen settle": one
 * wait, and not reported as a timeout, because nothing was being waited for.
 *
 * With a guard the loop ends on a match, on the deadline, or as soon as the
 * session stops changing — a stalled screen will not start satisfying the
 * guard, so burning the rest of the timeout on it only delays the answer.
 */
export async function sendAndExpect(
  session: ExpectSession,
  keys: string,
  options: SendAndExpectOptions = {},
): Promise<ExpectResult> {
  const payload = options.sanitize === false ? keys : prepareKeystrokes(keys);
  // Clamping a negative timeout is faithful rather than load-bearing: a
  // negative deadline is already in the past, so the loop reports a timeout
  // either way.
  // The clamp is faithful rather than load-bearing: a negative deadline is
  // already in the past, so the loop reports a timeout either way.
  const timeoutMs = Math.max(0, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  let since = session.screenChangeSeq();
  if (payload !== "") {
    await session.send(payload);
  }

  const deadline = performance.now() + timeoutMs;
  let screen = screenOf(session);
  const immediate = findMatch(screen, options.expectText, options.expectRegex);
  if (immediate !== undefined) {
    return { matched: true, matchedText: immediate, screen, timedOut: false };
  }

  if (options.expectText === undefined && options.expectRegex === undefined) {
    const remaining = Math.max(0, Math.trunc(deadline - performance.now()));
    await session.waitForScreenChange({ timeoutMs: remaining, since });
    return { matched: false, matchedText: undefined, screen: screenOf(session), timedOut: false };
  }

  for (;;) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) {
      return { matched: false, matchedText: undefined, screen, timedOut: true };
    }
    // Never zero: a zero-length wait would spin rather than yield.
    const changed = await session.waitForScreenChange({ timeoutMs: Math.max(1, Math.trunc(remaining)), since });
    screen = screenOf(session);
    const found = findMatch(screen, options.expectText, options.expectRegex);
    if (found !== undefined) {
      return { matched: true, matchedText: found, screen, timedOut: false };
    }
    if (!changed) {
      return { matched: false, matchedText: undefined, screen, timedOut: true };
    }
    since = session.screenChangeSeq();
  }
}
