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

/**
 * Key-aware redaction rule, ported from `provide.uterm.redaction`.
 *
 * A key is split into lower-case segments (camelCase split, so `accessToken`
 * is `access`, `token`; `-`, `.` and spaces act like `_`). It is sensitive when
 * any segment is in {@link SENSITIVE_KEY_SEGMENTS} or two adjacent segments form
 * a pair in {@link SENSITIVE_KEY_PAIRS} -- unless its last segment is in
 * {@link NON_SECRET_KEY_SUFFIXES} (`token_count`, `token_type` describe a
 * credential rather than hold one).
 */
export const SENSITIVE_KEY_SEGMENTS: ReadonlySet<string> = new Set([
  "token",
  "secret",
  "password",
  "passwd",
  "passphrase",
  "credential",
  "credentials",
  "cookie",
  "authorization",
  "apikey",
]);

/** Adjacent segment pairs that name a credential (`api_key`, `privateKey`, `access_key`). */
export const SENSITIVE_KEY_PAIRS: readonly (readonly [string, string])[] = [
  ["api", "key"],
  ["private", "key"],
  ["access", "key"],
];

/** A key whose last segment is one of these describes a credential and stays visible. */
export const NON_SECRET_KEY_SUFFIXES: ReadonlySet<string> = new Set([
  "count",
  "len",
  "length",
  "size",
  "type",
  "kind",
  "ttl",
  "expiry",
  "expires",
  "hash",
  "required",
  "enabled",
]);

function isUpper(ch: string): boolean {
  return ch >= "A" && ch <= "Z";
}

function isLowerOrDigit(ch: string): boolean {
  return (ch >= "a" && ch <= "z") || (ch >= "0" && ch <= "9");
}

/** Split a key into lower-case segments, exactly as the reference `key_segments` does. */
export function keySegments(key: string): string[] {
  let out = "";
  const last = key.length - 1;
  for (let i = 0; i < key.length; i++) {
    const ch = key.charAt(i);
    if (i > 0 && isUpper(ch)) {
      const prev = key.charAt(i - 1);
      const next = i < last ? key.charAt(i + 1) : "";
      if (isLowerOrDigit(prev) || (isUpper(prev) && next >= "a" && next <= "z")) {
        out += "_";
      }
    }
    out += ch;
  }
  return out
    .toLowerCase()
    .replaceAll("-", "_")
    .replaceAll(".", "_")
    .replaceAll(" ", "_")
    .split("_")
    .filter((segment) => segment.length > 0);
}

/** Whether `key` names a credential field (see {@link SENSITIVE_KEY_SEGMENTS}). */
export function isSensitiveKey(key: string): boolean {
  const segments = keySegments(key);
  const lastSegment = segments.at(-1);
  if (lastSegment === undefined || NON_SECRET_KEY_SUFFIXES.has(lastSegment)) {
    return false;
  }
  if (segments.some((segment) => SENSITIVE_KEY_SEGMENTS.has(segment))) {
    return true;
  }
  return segments.some((segment, i) =>
    SENSITIVE_KEY_PAIRS.some(([first, second]) => segment === first && segments[i + 1] === second),
  );
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

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const proto: unknown = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function walk(value: unknown, redactor: Redactor, keyAware: boolean, maxDepth: number, depth: number): unknown {
  if (typeof value === "string") {
    return redactor(value);
  }
  if (!Array.isArray(value) && !isPlainObject(value)) {
    // Scalars, and objects that are not JSON-shaped records (Date, Map, Set,
    // class instances): passed through unchanged rather than flattened to {}.
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
