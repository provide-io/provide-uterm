//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * `Annotation.matchKey`: a hashed (rule, matched text) identity, never
 * serialised. Mirrors the reference's `tests/test_annotation_match_key.py`.
 */

import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { type Annotation, annotationMatchKey, annotationToWire, type DetectionRule, PatternDetector } from "./index.ts";

const KEY_A = "AKIAIOSFODNN7EXAMPLE"; // pragma: allowlist secret
const KEY_B = "AKIAJ2345678ABCDWXYZ"; // pragma: allowlist secret

/** The reference's digest, computed independently of the port. */
function sha256Hex(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

describe("the match key", () => {
  it("is the SHA-256 of the rule id, a NUL and the full match, lowercase hex", () => {
    const expected = sha256Hex(Buffer.from(`cred.aws_access_key\x00${KEY_A}`, "utf8"));
    expect(annotationMatchKey("cred.aws_access_key", KEY_A)).toBe(expected);
    expect(expected).toMatch(/^[0-9a-f]{64}$/);
  });

  it.each([
    ["one byte", "a", [0x61]],
    ["two bytes", "é", [0xc3, 0xa9]],
    ["three bytes", "☃", [0xe2, 0x98, 0x83]],
    ["four bytes, from a surrogate pair", "😀", [0xf0, 0x9f, 0x98, 0x80]],
    // `surrogatepass`: a lone surrogate hashes as its own three bytes rather
    // than as U+FFFD, which is what a UTF-8 encoder would substitute.
    ["a lone surrogate, passed through", "\ud800", [0xed, 0xa0, 0x80]],
    ["the last code point of each width", "\x7f߿￿", [0x7f, 0xdf, 0xbf, 0xef, 0xbf, 0xbf]],
    ["the first code point of each width", "\x80ࠀ\u{10000}", [0xc2, 0x80, 0xe0, 0xa0, 0x80, 0xf0, 0x90, 0x80, 0x80]],
    ["the last code point there is", "\u{10ffff}", [0xf4, 0x8f, 0xbf, 0xbf]],
  ])("encodes %s as the reference's UTF-8 does", (_name, match, bytes) => {
    const expected = sha256Hex(Uint8Array.from([0x72, 0x00, ...bytes]));
    expect(annotationMatchKey("r", match)).toBe(expected);
  });

  it("tells two different credentials of one rule apart", () => {
    const detector = new PatternDetector();
    const [first] = detector.detect("read", `export K=${KEY_A}`, 1) as [Annotation];
    const [second] = detector.detect("read", `export K=${KEY_B}`, 2) as [Annotation];
    // Same rule, same description: only the match key tells them apart.
    expect(first.description).toBe(second.description);
    expect(first.matchKey).toBe(annotationMatchKey("cred.aws_access_key", "AKIAIOSFODNN7EXA"));
    expect(second.matchKey).toBe(annotationMatchKey("cred.aws_access_key", "AKIAJ2345678ABCD"));
    expect(first.matchKey).not.toBe(second.matchKey);
  });

  it("is the same for the same match wherever it appears", () => {
    const detector = new PatternDetector();
    const [first] = detector.detect("read", "$ sudo ls", 1) as [Annotation];
    const [second] = detector.detect("send", "later: sudo ls -l", 9) as [Annotation];
    expect(first.matchKey).toBe(second.matchKey);
  });

  it("hashes the full match, not the description's 80-character cut", () => {
    const rule: DetectionRule = {
      ruleId: "long",
      label: "long",
      pattern: /x+/,
      severity: "info",
      descriptionTemplate: "{match}",
      eventTypes: new Set(["send"]),
      category: "long",
    };
    const text = "x".repeat(200);
    const [annotation] = new PatternDetector([rule]).detect("send", text, 1) as [Annotation];
    expect(annotation.description).toBe("x".repeat(80));
    expect(annotation.matchKey).toBe(annotationMatchKey("long", text));
  });

  it("is not serialised, and not part of equality", () => {
    const [annotation] = new PatternDetector().detect("read", KEY_A, 1) as [Annotation];
    expect(annotation.matchKey).toHaveLength(64);
    expect(annotationToWire(annotation)).not.toHaveProperty("matchKey");
    expect(Object.keys(annotation)).not.toContain("matchKey");
    expect(JSON.stringify(annotation)).not.toContain(annotation.matchKey as string);
    expect(JSON.stringify(annotationToWire(annotation))).not.toContain(KEY_A);
    const other: Annotation = {
      label: annotation.label,
      description: annotation.description,
      severity: annotation.severity,
      source: annotation.source,
      principal: annotation.principal,
      span: annotation.span,
    };
    expect(other.matchKey).toBeUndefined();
    expect(annotation).toStrictEqual(other);
  });
});
