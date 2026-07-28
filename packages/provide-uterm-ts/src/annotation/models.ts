//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What an annotation is.
 *
 * Port of the Python module `provide.uterm.annotation._models`.
 */

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
