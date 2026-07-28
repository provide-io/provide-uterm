//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Reject regexes that could backtrack catastrophically.
 *
 * Port of `_validate_pattern_safety` in the Python module
 * `provide.uterm.server.bridge.hub.event_bus` and the Go package `hub`
 * (`patternsafety.go`).
 *
 * The patterns this guards are searched against a whole screen inside the
 * hijack poll loop, so `(a+)+` is not a slow query — it pins the event loop
 * and stalls every other session on the hub. This runs before compilation,
 * because by the time a pattern is compiled the damage is already available
 * to whoever supplied it.
 *
 * The rule is deliberately narrow: a quantifier applied to a group is refused
 * when that group already contained a quantifier, or contained an
 * alternation. Both conditions propagate outward through nesting, so
 * `(?=(a+))+` and `((a|b))+` are caught as well. Everything else compiles.
 */

import { pyIsDigit } from "../pycompat/index.ts";

/** Raised when a pattern is rejected as a backtracking risk. */
export class UnsafePatternError extends Error {}

/** What the scanner last consumed, which decides what a quantifier applies to. */
type PreviousKind = "" | "literal" | "group" | "alternation" | "quantifier";

/** Per-group tracking: did it contain a quantifier, or an alternation? */
interface GroupFrame {
  hasInnerQuantifier: boolean;
  hasAlternation: boolean;
}

/**
 * Whether `{...}` starting at `start` is a counted quantifier.
 *
 * A literal brace is common in prompt guards, so this has to be a sniff
 * rather than an assumption. The body must be digits, optionally followed by
 * a comma and either nothing or more digits.
 *
 * "Digits" is CPython's definition, not ASCII: a repeat count written in
 * Arabic-Indic or superscript digits still makes this a quantifier, and
 * missing that would let `(a+){٣}` past the nesting rule below.
 */
function looksLikeCountedQuantifier(pattern: string, start: number): boolean {
  const end = pattern.indexOf("}", start + 1);
  if (end === -1) {
    return false;
  }
  const body = pattern.slice(start + 1, end);
  if (body === "") {
    return false;
  }
  const comma = body.indexOf(",");
  if (comma === -1) {
    return pyIsDigit(body);
  }
  const left = body.slice(0, comma);
  const right = body.slice(comma + 1);
  return pyIsDigit(left) && (right === "" || pyIsDigit(right));
}

/**
 * Skip a group's prefix — `(?:`, `(?=`, `(?!`, `(?<=`, `(?<!`, `(?P<name>`.
 *
 * The marker characters are not content, and counting them would make the
 * scanner think a lookahead's `=` was a literal sitting between the group and
 * its quantifier.
 *
 * Returns the index of the first character of the group body.
 *
 * Inert for well-formed patterns — the prefix characters are not
 * metacharacters, so treating them as literals reaches the same verdict —
 * but kept because the reference has it and a future prefix form need not
 * be as harmless.
 */
function skipGroupPrefix(pattern: string, index: number): number {
  let i = index;
  if (i >= pattern.length || pattern[i] !== "?") {
    return i;
  }
  i += 1;
  if (pattern[i] === "<" && (pattern[i + 1] === "=" || pattern[i + 1] === "!")) {
    return i + 2;
  }
  if (pattern[i] === "P") {
    const end = pattern.indexOf(">", i);
    return end === -1 ? i : end + 1;
  }
  if (pattern[i] === ":" || pattern[i] === "=" || pattern[i] === "!") {
    return i + 1;
  }
  // Anything else — an inline flag group like `(?i)` — is left alone.
  return i;
}

/**
 * Reject `pattern` if applying a quantifier to a group would risk
 * catastrophic backtracking.
 *
 * @throws {UnsafePatternError} With a message naming which rule was hit;
 *   callers surface it, and the two cases describe different mistakes.
 */
export function validatePatternSafety(pattern: string): void {
  const groupStack: GroupFrame[] = [];
  let previousKind: PreviousKind = "";
  let lastClosedHadQuantifier = false;
  let lastClosedHadAlternation = false;
  let escaped = false;
  let inClass = false;
  let i = 0;

  while (i < pattern.length) {
    const char = pattern[i] as string;

    if (escaped) {
      escaped = false;
      previousKind = "literal";
      i += 1;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      i += 1;
      continue;
    }
    if (inClass) {
      // Nothing inside a class is structural: `[(a+)]` is four literals.
      if (char === "]") {
        inClass = false;
        previousKind = "literal";
      }
      i += 1;
      continue;
    }
    if (char === "[") {
      inClass = true;
      i += 1;
      continue;
    }
    if (char === "(") {
      groupStack.push({ hasInnerQuantifier: false, hasAlternation: false });
      previousKind = "";
      lastClosedHadQuantifier = false;
      lastClosedHadAlternation = false;
      i = skipGroupPrefix(pattern, i + 1);
      continue;
    }
    if (char === ")" && groupStack.length > 0) {
      const frame = groupStack.pop() as GroupFrame;
      lastClosedHadQuantifier = frame.hasInnerQuantifier;
      lastClosedHadAlternation = frame.hasAlternation;
      // Propagate outward, so a parent wrapping a risky subgroup is itself
      // risky once a quantifier is applied to it.
      const parent = groupStack.at(-1);
      if (parent !== undefined) {
        parent.hasInnerQuantifier ||= frame.hasInnerQuantifier;
        parent.hasAlternation ||= frame.hasAlternation;
      }
      previousKind = "group";
      i += 1;
      continue;
    }
    if (char === "|") {
      const frame = groupStack.at(-1);
      if (frame !== undefined) {
        frame.hasAlternation = true;
      }
      previousKind = "alternation";
      i += 1;
      continue;
    }
    if (char === "+" || char === "*" || (char === "{" && looksLikeCountedQuantifier(pattern, i))) {
      if (previousKind === "group") {
        if (lastClosedHadQuantifier) {
          throw new UnsafePatternError("unsafe watch pattern: nested quantified groups are not allowed");
        }
        if (lastClosedHadAlternation) {
          throw new UnsafePatternError(
            "unsafe watch pattern: quantified groups containing alternation are not allowed",
          );
        }
      }
      const frame = groupStack.at(-1);
      if (frame !== undefined) {
        frame.hasInnerQuantifier = true;
      }
      previousKind = "quantifier";
      i = char === "{" ? pattern.indexOf("}", i) + 1 : i + 1;
      continue;
    }

    // A literal between a group and a quantifier means the repeat applies to
    // the literal, so the group stops mattering.
    previousKind = "literal";
    lastClosedHadQuantifier = false;
    lastClosedHadAlternation = false;
    i += 1;
  }
}
