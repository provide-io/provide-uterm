//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What an annotation is.
 *
 * Port of the Python module `provide.uterm.annotation._models`.
 */

import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

/** A contiguous range of recording sequence numbers. */
export interface AnnotationSpan {
  fromSeq: number;
  toSeq: number;
}

/** An interesting moment in a recording. */
export interface Annotation {
  /** What kind of thing this is. */
  label: string;
  /** What it says to a reviewer. */
  description: string;
  /** How much it matters. */
  severity: string;
  /** What produced it. */
  source: string;
  /** Who it is attributed to. */
  principal: string;
  /** Where in the recording, when it is a range. */
  span?: AnnotationSpan | undefined;
  /**
   * {@link annotationMatchKey} of the rule and the full text it matched, for
   * an annotation the detector produced; absent on any other.
   *
   * The reference's `Annotation.match_key`, which is excluded from `to_dict`,
   * `repr` and equality: an in-process identity for dedupe, not recorded
   * data. So the detector sets it as a non-enumerable property — it is not in
   * {@link annotationToWire}, a JSON dump, a spread or a deep-equality check.
   */
  readonly matchKey?: string | undefined;
}

/** A compiled rule the detector scans with. */
export interface DetectionRule {
  /** Identifies the rule. */
  ruleId: string;
  /** The label an annotation gets. */
  label: string;
  /** What it looks for. */
  pattern: RegExp;
  /** How much a match matters. */
  severity: string;
  /** How the description reads, with `{match}` and `{event_type}`. */
  descriptionTemplate: string;
  /** Which streams it applies to. */
  eventTypes: ReadonlySet<string>;
  /** Only the first rule to match a category produces an annotation. */
  category: string;
}

/**
 * `text` as UTF-8, with a lone surrogate encoded as its own three bytes.
 *
 * Python's `encode("utf-8", "surrogatepass")`: a string can hold a lone
 * surrogate, and the reference hashes it rather than raising. `TextEncoder`
 * would substitute U+FFFD instead, which would give a different key.
 */
function utf8SurrogatePass(text: string): Uint8Array {
  const bytes: number[] = [];
  for (const char of text) {
    // A paired surrogate iterates as one code point; a lone one as itself.
    const code = char.codePointAt(0) as number;
    if (code < 0x80) {
      bytes.push(code);
    } else if (code < 0x800) {
      bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code < 0x10000) {
      bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    } else {
      bytes.push(0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    }
  }
  return Uint8Array.from(bytes);
}

/**
 * Identity of one detector match: lowercase hex SHA-256 of the UTF-8 bytes of
 * `ruleId`, one NUL byte, then the UTF-8 bytes of `match`.
 *
 * The reference's `annotation_match_key`. `match` is the FULL matched text,
 * not the 80-character description cut. The digest names the rule and the
 * exact text it matched — two different AWS keys get two different keys, the
 * same one seen twice gets the same — without the raw text, which is often a
 * secret, being kept anywhere.
 */
export function annotationMatchKey(ruleId: string, match: string): string {
  return bytesToHex(sha256(utf8SurrogatePass(`${ruleId}\x00${match}`)));
}

/** An annotation as it goes over the wire. */
export function annotationToWire(annotation: Annotation): Record<string, unknown> {
  return {
    label: annotation.label,
    description: annotation.description,
    severity: annotation.severity,
    source: annotation.source,
    principal: annotation.principal,
    // Always present, so a consumer need not distinguish absent from null.
    span: annotation.span === undefined ? null : { from_seq: annotation.span.fromSeq, to_seq: annotation.span.toSeq },
  };
}
