//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Heuristic input-type detection from terminal screen text.
 *
 * Port of the Python module `provide.uterm.detection.input_type` and the Go
 * package `detection`.
 */

/** What a prompt expects the user to send. */
export type InputType = "any_key" | "single_key" | "multi_key";

/** Phrases meaning "press anything to continue". */
const ANY_KEY_PHRASES = [
  "press any key",
  "press a key",
  "hit any key",
  "strike any key",
  "<more>",
  "[more]",
  "-- more --",
] as const;

/** Phrases meaning "one keystroke decides". */
const SINGLE_KEY_PHRASES = [
  "(y/n)",
  "(yes/no)",
  "continue?",
  "quit?",
  "abort?",
  "retry?",
  "[y/n]",
  "(q)uit",
  "(a)bort",
] as const;

/** Phrases meaning "type a line". */
const MULTI_KEY_PHRASES = [
  "enter",
  "type",
  "input",
  "name:",
  "password:",
  "username:",
  "choose:",
  "select:",
  "command:",
  "search:",
] as const;

/**
 * Heuristically detect the input type a prompt is asking for.
 *
 * The three phrase lists are checked in order and the first hit wins, so a
 * screen carrying phrases from two tiers resolves to the earlier one — a
 * "press any key, then enter your name" screen is `any_key`. Matching is
 * case-insensitive and by substring, so a phrase embedded in a longer word
 * still counts. Anything unmatched falls through to `multi_key`.
 */
export function autoDetectInputType(screen: string): InputType {
  const lowered = screen.toLowerCase();
  if (ANY_KEY_PHRASES.some((phrase) => lowered.includes(phrase))) {
    return "any_key";
  }
  if (SINGLE_KEY_PHRASES.some((phrase) => lowered.includes(phrase))) {
    return "single_key";
  }
  // The final tier and the fallback are the same answer; the check is kept
  // so the tier structure stays visible rather than collapsing to a return.
  if (MULTI_KEY_PHRASES.some((phrase) => lowered.includes(phrase))) {
    return "multi_key";
  }
  return "multi_key";
}
