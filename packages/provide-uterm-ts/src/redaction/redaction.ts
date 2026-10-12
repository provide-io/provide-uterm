//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Reusable redaction helpers for terminal logs and captures.
 *
 * Port of the Python module `provide.uterm.redaction`, the Go package
 * `redaction` and the C# `Provide.Uterm.Redaction`.
 */

import { compilePyPattern } from "../pycompat/index.ts";

/** Rewrites text, replacing sensitive spans. */
export type Redactor = (text: string) => string;

/** The literal text every matched span is replaced with. */
const REPLACEMENT = "[REDACTED]";

/**
 * Build a text redactor from regex patterns.
 *
 * With no patterns the returned redactor is the identity function — a
 * distinct path from "patterns that happen not to match", and the one the
 * hot path relies on. Patterns are applied in order, each seeing the output
 * of the one before it.
 *
 * @throws {Error} If a pattern fails to compile, matching CPython's
 *   `re.error` at construction time rather than at first use.
 */
export function makeRedactor(patterns: readonly string[] = []): Redactor {
  const compiled = patterns.map((pattern) => compilePyPattern(pattern));
  if (compiled.length === 0) {
    return (text) => text;
  }
  return (text) => {
    let result = text;
    for (const pattern of compiled) {
      // A fresh RegExp per substitution keeps `lastIndex` from leaking
      // between calls, and the replacement is passed as a function so `$&`
      // and friends are never re-expanded.
      result = result.replace(new RegExp(pattern.source, pattern.flags), () => REPLACEMENT);
    }
    return result;
  };
}

/** Apply `redactor` to `text`, preserving identity when none is configured. */
export function redactText(text: string, redactor: Redactor | null | undefined): string {
  if (redactor === null || redactor === undefined) {
    return text;
  }
  return redactor(text);
}

/**
 * Containers nested this deep are not walked by {@link redactStructure}.
 *
 * Mirrors `REDACT_MAX_DEPTH` in the reference: real frames are two or three
 * levels deep, and the cap keeps a hostile frame nested thousands deep from
 * blowing the stack in every recorder that redacts it.
 */
export const REDACT_MAX_DEPTH = 32;

/**
 * What a container at or past {@link REDACT_MAX_DEPTH} becomes. The subtree
 * is replaced whole, never returned unwalked — an unwalked subtree is an
 * unredacted one.
 */
export const DEPTH_LIMIT_MARKER = "[NESTED_REDACTED]";

/** What the value of a sensitive key becomes under key-aware redaction. */
export const SENSITIVE_KEY_MARKER = "[SECRET_REDACTED]";

/** Field names whose value is a credential regardless of what it looks like. */
export const SENSITIVE_KEY_NAMES: ReadonlySet<string> = new Set([
  "token",
  "password",
  "passwd",
  "passphrase",
  "secret",
  "api_key",
  "apikey",
  "authorization",
  "cookie",
  "private_key",
]);

/** Any key ending in one of these is sensitive too (resume_token, client_secret, ...). */
export const SENSITIVE_KEY_SUFFIXES: readonly string[] = ["_token", "_password", "_passphrase", "_secret", "_api_key"];

/** Whether `key` names a credential field: lower-cased, `-` folded to `_`. */
export function isSensitiveKey(key: string): boolean {
  const folded = key.toLowerCase().replaceAll("-", "_");
  return SENSITIVE_KEY_NAMES.has(folded) || SENSITIVE_KEY_SUFFIXES.some((suffix) => folded.endsWith(suffix));
}

/** Options for {@link redactStructure}. */
export interface RedactStructureOptions {
  /** Mask the value of every sensitive key (see {@link isSensitiveKey}). */
  keyAware?: boolean;
  /** Override the depth cap. */
  maxDepth?: number;
}

/**
 * Return a redacted copy of a JSON-like structure.
 *
 * Port of `provide.uterm.redaction.redact_structure`: strings go through
 * `redactor` at any depth; arrays and objects are rebuilt (keys kept, input
 * never mutated); a container `maxDepth` levels below the root becomes
 * {@link DEPTH_LIMIT_MARKER}; with `keyAware`, a non-null value under a
 * sensitive key becomes {@link SENSITIVE_KEY_MARKER}; other scalars pass
 * through. Recursion is bounded by the cap, so no input can overflow the stack.
 */
export function redactStructure(value: unknown, redactor: Redactor, options: RedactStructureOptions = {}): unknown {
  return walk(value, redactor, options.keyAware ?? false, options.maxDepth ?? REDACT_MAX_DEPTH, 0);
}

function walk(value: unknown, redactor: Redactor, keyAware: boolean, maxDepth: number, depth: number): unknown {
  if (typeof value === "string") {
    return redactor(value);
  }
  if (typeof value !== "object" || value === null) {
    return value;
  }
  if (depth >= maxDepth) {
    return DEPTH_LIMIT_MARKER;
  }
  const child = depth + 1;
  if (Array.isArray(value)) {
    return value.map((item: unknown) => walk(item, redactor, keyAware, maxDepth, child));
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      keyAware && item !== null && item !== undefined && isSensitiveKey(key)
        ? SENSITIVE_KEY_MARKER
        : walk(item, redactor, keyAware, maxDepth, child),
    ]),
  );
}
