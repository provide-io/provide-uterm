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
