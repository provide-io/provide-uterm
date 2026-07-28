//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The scanner that marks interesting moments in a recording.
 *
 * Port of the Python module `provide.uterm.annotation._detector`.
 *
 * Getting this wrong is quiet in both directions: a missed match means an
 * incident review never sees the moment, and a leaked one means the secret
 * itself ends up in the annotation, which flows to telemetry and logs.
 */

import type { Annotation, DetectionRule } from "./models.ts";
import { BUILTIN_RULES } from "./rules.ts";

/** How much of a match reaches the description. */
const DESCRIPTION_TRUNCATE = 80;

/** Stands in for the match when a template could not be formatted. */
const FALLBACK_PLACEHOLDER = "<unavailable>";

/** What a scan found, and how far into the text it got. */
export interface ScanResult {
  /** At most one annotation per category. */
  annotations: Annotation[];
  /** The end offset of the furthest match, or zero when nothing matched. */
  matchEnd: number;
}

/**
 * Fill in a description template.
 *
 * @returns The formatted description, or nothing when the template names a
 *   field this does not supply.
 */
function formatDescription(template: string, match: string, eventType: string): string | undefined {
  let failed = false;
  const description = template.replace(/\{([^{}]*)\}/g, (_whole, field: string) => {
    if (field === "match") {
      return match;
    }
    if (field === "event_type") {
      return eventType;
    }
    failed = true;
    return "";
  });
  return failed ? undefined : description;
}

/** Scans terminal text against a set of rules. */
export class PatternDetector {
  readonly #rules: readonly DetectionRule[];

  constructor(rules?: readonly DetectionRule[]) {
    this.#rules = rules ?? BUILTIN_RULES;
  }

  /** Everything `text` matches, at most one annotation per category. */
  detect(eventType: string, text: string, seq: number): Annotation[] {
    return this.scan(eventType, text, seq).annotations;
  }

  /**
   * Like {@link detect}, but also reporting how far the furthest match ran.
   *
   * {@link StreamingDetector} uses the offset to carry only the tail *after*
   * a completed match, so the match is not reported twice while a second
   * secret starting right after it can still bridge the next boundary.
   */
  scan(eventType: string, text: string, seq: number): ScanResult {
    // The hot path. Not load-bearing on its own — a loop over no matches
    // returns the same thing — but this is called on every chunk of every
    // stream, and the empty case is common.
    if (text === "") {
      return { annotations: [], matchEnd: 0 };
    }

    const annotations: Annotation[] = [];
    const seenCategories = new Set<string>();
    let matchEnd = 0;

    for (const rule of this.#rules) {
      // Only the first rule to match a category counts: otherwise one line
      // mentioning a password produces four near-identical annotations and
      // buries the timeline.
      if (seenCategories.has(rule.category) || !rule.eventTypes.has(eventType)) {
        continue;
      }
      const match = rule.pattern.exec(text);
      if (match === null) {
        continue;
      }

      seenCategories.add(rule.category);
      matchEnd = Math.max(matchEnd, match.index + match[0].length);
      const matchText = match[0].slice(0, DESCRIPTION_TRUNCATE);
      // A malformed template must not leak the raw match — which is the
      // secret — into a description that flows to telemetry and logs.
      const description =
        formatDescription(rule.descriptionTemplate, matchText, eventType) ?? `${rule.label}: ${FALLBACK_PLACEHOLDER}`;
      annotations.push({
        label: rule.label,
        description,
        severity: rule.severity,
        source: "detector",
        principal: "system",
        span: { fromSeq: seq, toSeq: seq },
      });
    }

    return { annotations, matchEnd };
  }
}
