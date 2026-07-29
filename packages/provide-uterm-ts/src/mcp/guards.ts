//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The guards every MCP tool funnels caller-supplied input through.
 *
 * Port of `provide.uterm.ai.patterns` and the validator half of
 * `provide.uterm.ai.server_validators`. "Caller-supplied" here means
 * LLM-supplied, and a `viewer` — the lowest role there is — can reach these
 * through `session_watch` and `session_subscribe`.
 *
 * Two things happen before a pattern is compiled. A length cap removes the
 * cheap amplification path, and a structural denylist refuses the classic
 * exponential-backtracking shapes a short pattern can still carry: a
 * quantified group whose body is itself quantified, and a quantified
 * backreference. Neither runtime bounds matching time — a linear-time engine
 * is not a dependency of either port — so this is a denylist rather than a
 * proof. It does **not** catch overlapping alternations like `(a|a)*`, and
 * the tests say so out loud; the server's own egress and time bounds remain
 * the backstop.
 *
 * The rest is contract: a bad pattern or a bad id comes back as a structured
 * refusal rather than an exception, because an exception reaches the caller
 * as a tool error and says more about this process than a refusal does.
 */

import { safeId } from "../client/hijack-guards.ts";
import { compilePyPattern } from "../pycompat/regex.ts";
import { MAX_USER_PATTERN_LEN } from "./policy.ts";

export { MAX_USER_PATTERN_LEN };

/** A backreference token: `\1` through `\99`, which is all the engine allows. */
const BACKREF = /^\\[1-9][0-9]?$/;

/** A backreference with a quantifier hung off it, anywhere in the pattern. */
const QUANTIFIED_BACKREF = /\\[1-9][0-9]?[+*{]/;

/** The characters that make the group they follow a repeated one. */
const QUANTIFIER_OPENERS = new Set(["+", "*", "{"]);

/** What a tool answers instead of throwing. */
export interface McpRejection {
  success: false;
  error: string;
  detail: string;
}

/**
 * Every balanced `(...)` group, with the character that follows its close.
 *
 * An escaped paren is a literal, not a delimiter. An unbalanced close is
 * ignored: the engine rejects the pattern later anyway, and this runs first,
 * so it sees malformed input as a matter of course and must not throw on it.
 */
function groupBodiesWithFollowingChar(pattern: string): Array<[string, string]> {
  const stack: number[] = [];
  const results: Array<[string, string]> = [];
  let index = 0;
  // Reading one past the end would find nothing rather than misread
  // something, so the bound is a statement of intent as much as a guard.
  while (index < pattern.length) {
    const character = pattern[index];
    if (character === "\\") {
      // Past the escaped character, so `\(` and `\)` are literals.
      index += 2;
      continue;
    }
    if (character === "(") {
      stack.push(index);
    } else if (character === ")" && stack.length > 0) {
      const start = stack.pop() as number;
      results.push([pattern.slice(start + 1, index), pattern[index + 1] ?? ""]);
    }
    index += 1;
  }
  return results;
}

/**
 * Whether a group's body is itself a repeated unit.
 *
 * Inside a group that is also quantified, such a body is the nested-quantifier
 * shape — or, for a lone backreference, the quantified-backref-in-a-group one.
 */
function bodyIsRepeatedUnit(body: string): boolean {
  // Kept because the reference says the rule out loud, though nothing can
  // tell it apart from outside: an empty body has no last character, and
  // every test below then answers no anyway.
  if (body === "") {
    return false;
  }
  if (BACKREF.test(body)) {
    return true;
  }
  const last = body[body.length - 1] as string;
  // A lazy quantifier ends in `?`; the quantifier itself is the character
  // before it.
  if (last === "?") {
    return body.length >= 2 && "+*}".includes(body[body.length - 2] as string);
  }
  if (!"+*}".includes(last)) {
    return false;
  }
  // An escaped quantifier is a literal, not a repetition.
  return !(body.length >= 2 && body[body.length - 2] === "\\");
}

/**
 * Whether a pattern carries a shape known to backtrack catastrophically.
 *
 * A structural denylist, not a proof of linear-time matching — see the module
 * comment for what it lets through.
 */
export function hasCatastrophicConstruct(pattern: string): boolean {
  if (QUANTIFIED_BACKREF.test(pattern)) {
    return true;
  }
  for (const [body, after] of groupBodiesWithFollowingChar(pattern)) {
    if (QUANTIFIER_OPENERS.has(after) && bodyIsRepeatedUnit(body)) {
      return true;
    }
  }
  return false;
}

/**
 * Compile a pattern somebody else wrote, behind the length and shape guards.
 *
 * @throws {Error} With the reference's wording for a pattern that is too long
 *   or catastrophically shaped. What counts as a *valid* pattern, and how the
 *   engine words its complaint, is this runtime's own.
 */
export function compileUserPattern(pattern: string): RegExp {
  // Length first: a long pattern costs a comparison rather than a scan.
  if (pattern.length > MAX_USER_PATTERN_LEN) {
    throw new Error(`pattern too long (max ${MAX_USER_PATTERN_LEN} chars)`);
  }
  if (hasCatastrophicConstruct(pattern)) {
    throw new Error(
      "pattern rejected: catastrophic-backtracking construct (nested quantifier or quantified backreference)",
    );
  }
  try {
    // Through the CPython-dialect compiler, so a pattern an operator wrote
    // against the reference — a leading `(?i)`, say — compiles rather than
    // failing outright.
    return compilePyPattern(pattern);
  } catch (error) {
    throw new Error(`invalid pattern: ${(error as Error).message}`);
  }
}

/** A refusal for a pattern this will not take, or nothing. */
export function rejectBadPattern(pattern: string | null | undefined): McpRejection | undefined {
  // No pattern is not a bad pattern: a tool asked for no filter has nothing
  // to refuse.
  if (pattern === null || pattern === undefined) {
    return undefined;
  }
  try {
    compileUserPattern(pattern);
  } catch (error) {
    return { success: false, error: "invalid_pattern", detail: (error as Error).message };
  }
  // Spelled out, as the reference spells `return None`, though falling off
  // the end would answer the same.
  return undefined;
}

/**
 * A pattern compiled once, or the refusal for it.
 *
 * Returned together so a tool can validate and then use the same compiled
 * pattern. Validating and recompiling would run the guard twice and give a
 * caller two chances to be told different things.
 */
export function compiledPatternOrRejection(
  pattern: string | null | undefined,
): [RegExp | undefined, McpRejection | undefined] {
  if (pattern === null || pattern === undefined) {
    return [undefined, undefined];
  }
  try {
    return [compileUserPattern(pattern), undefined];
  } catch (error) {
    return [undefined, { success: false, error: "invalid_pattern", detail: (error as Error).message }];
  }
}

/**
 * A refusal for an id that is not one safe path segment, or nothing.
 *
 * The same allow-list {@link safeId} enforces, so the path-injection
 * guarantee is unchanged — this only turns the refusal into the structured
 * contract every other guard here uses.
 */
export function rejectBadId(value: string, kind = "id"): McpRejection | undefined {
  try {
    safeId(value, kind);
  } catch (error) {
    return { success: false, error: "invalid_id", detail: (error as Error).message };
  }
  return undefined;
}

/**
 * The first refusal among several `(value, kind)` ids, or nothing.
 *
 * In the order they were given, so a caller told about a bad `worker_id`
 * fixes that one rather than hearing about the `hijack_id` behind it.
 */
export function rejectBadIds(...pairs: Array<[string, string]>): McpRejection | undefined {
  for (const [value, kind] of pairs) {
    const rejection = rejectBadId(value, kind);
    if (rejection !== undefined) {
      return rejection;
    }
  }
  return undefined;
}
