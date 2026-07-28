//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type Annotation,
  annotationToWire,
  BUILTIN_RULES,
  DEFAULT_MAX_CARRY,
  type DetectionRule,
  PatternDetector,
  StreamingDetector,
} from "./index.ts";

interface AnnotationGolden {
  aws_key: string;
  rules: Array<{
    rule_id: string;
    label: string;
    pattern: string;
    severity: string;
    description_template: string;
    event_types: string[];
    category: string;
  }>;
  categories_in_order: string[];
  scans: Array<{
    name: string;
    event_type: string;
    text: string;
    annotations: Array<Record<string, unknown>>;
    match_end: number;
    detect_matches_scan: boolean;
  }>;
  streams: Array<{ name: string; steps: Array<{ chunk: string; annotations: Array<Record<string, unknown>> }> }>;
  bounded_carry: { too_small_to_bridge: unknown[]; large_enough: unknown[] };
  carry: {
    an_empty_chunk_produces_nothing: unknown[];
    a_reset_forgets_the_tail: unknown[];
    without_a_reset_it_bridges: unknown[];
    default_max_carry: number;
  };
  templates: Record<string, string | number>;
  empty_annotation: Record<string, unknown>;
  annotation_with_span: Record<string, unknown>;
}

const golden = loadGolden<AnnotationGolden>("annotation_golden.json");

/** A rule for testing template handling. */
function rule(descriptionTemplate: string, pattern = /secret-value/): DetectionRule {
  return {
    ruleId: "t",
    label: "test",
    pattern,
    severity: "low",
    descriptionTemplate,
    eventTypes: new Set(["read"]),
    category: "test",
  };
}

describe("the built-in rules", () => {
  it("match the reference, in order", () => {
    // The order is load-bearing: only the first rule to match a category
    // produces an annotation, so a reordering changes which one reports.
    expect(
      BUILTIN_RULES.map((entry) => ({
        rule_id: entry.ruleId,
        label: entry.label,
        severity: entry.severity,
        description_template: entry.descriptionTemplate,
        event_types: [...entry.eventTypes].sort(),
        category: entry.category,
      })),
    ).toStrictEqual(
      golden.rules.map((entry) => ({
        rule_id: entry.rule_id,
        label: entry.label,
        severity: entry.severity,
        description_template: entry.description_template,
        event_types: entry.event_types,
        category: entry.category,
      })),
    );
  });

  it("matches what each recorded pattern matches", () => {
    // Compiled behaviour rather than source text: JavaScript normalises a
    // pattern when it compiles it — `[\w.\-]` comes back as `[\w.-]` — so
    // comparing the text would fail on two rules that behave identically.
    // What has to hold is that the same input matches.
    const probes = [
      ...golden.scans.map((record) => record.text),
      ...golden.streams.flatMap((record) => record.steps.map((step) => step.chunk)),
      golden.aws_key,
      "ssh alice@host",
      "curl https://example.org",
      "rm -rf /",
    ];
    for (const [index, entry] of BUILTIN_RULES.entries()) {
      const recorded = golden.rules[index] as (typeof golden.rules)[number];
      const source = recorded.pattern.startsWith("(?i)") ? recorded.pattern.slice(4) : recorded.pattern;
      const reference = new RegExp(source, recorded.pattern.startsWith("(?i)") ? "i" : "");
      for (const probe of probes) {
        expect({ rule: entry.ruleId, probe, matched: entry.pattern.test(probe) }).toStrictEqual({
          rule: entry.ruleId,
          probe,
          matched: reference.test(probe),
        });
      }
    }
  });

  it("cover every category the reference covers", () => {
    expect([...new Set(BUILTIN_RULES.map((entry) => entry.category))]).toStrictEqual(golden.categories_in_order);
  });

  it("keeps the case-insensitive rule case-insensitive", () => {
    // The generic secret rule has to match PASSWORD= as well as password=,
    // and a lost flag would silently stop reporting the shouted form.
    const generic = BUILTIN_RULES.find((entry) => entry.ruleId === "cred.generic_secret");
    expect(generic?.pattern.flags).toContain("i");
    expect(new PatternDetector().detect("read", "PASSWORD = hunter2", 1)).toHaveLength(1);
  });
});

describe("scanning one chunk", () => {
  it.each(golden.scans)("$name", (record) => {
    const detector = new PatternDetector();
    const result = detector.scan(record.event_type, record.text, 7);
    expect(result.annotations.map(annotationToWire)).toStrictEqual(record.annotations);
    expect(result.matchEnd).toBe(record.match_end);
  });

  it("returns nothing for empty text", () => {
    // The hot path: no allocation beyond the result.
    const record = golden.scans.find((entry) => entry.name === "empty");
    expect(record?.annotations).toStrictEqual([]);
    expect(record?.match_end).toBe(0);
  });

  it("reports at most one annotation per category", () => {
    // Otherwise one line mentioning a password produces four near-identical
    // annotations and buries the timeline.
    const record = golden.scans.find((entry) => entry.name === "the same category twice");
    expect(record?.annotations).toHaveLength(1);
  });

  it("reports each category that matched", () => {
    const record = golden.scans.find((entry) => entry.name === "two categories at once");
    expect(record?.annotations.map((entry) => entry.label).sort()).toStrictEqual([
      "credential_exposure",
      "privilege_escalation",
    ]);
  });

  it("ignores a rule that does not apply to the event type", () => {
    // A resize carries no text a rule was written for.
    const record = golden.scans.find((entry) => entry.name === "an event type nothing applies to");
    expect(record?.annotations).toStrictEqual([]);
  });

  it("gives detect and scan the same annotations", () => {
    expect(golden.scans.every((record) => record.detect_matches_scan)).toBe(true);
  });

  it("spans the single sequence it was given", () => {
    const record = golden.scans.find((entry) => entry.name === "an aws key");
    expect(record?.annotations[0]?.span).toStrictEqual({ from_seq: 7, to_seq: 7 });
  });

  it("attributes what it found to the detector", () => {
    // Not to a person: an annotation a reviewer sees has to say where it came
    // from, and nobody typed this one.
    const record = golden.scans.find((entry) => entry.name === "an aws key");
    expect(record?.annotations[0]?.source).toBe("detector");
    expect(record?.annotations[0]?.principal).toBe("system");
  });

  it("reports the furthest match, not the first", () => {
    // The credential rule comes first in the list but matches earlier here,
    // so the furthest match belongs to a later rule. Keeping the first offset
    // would have the streaming wrapper carry text a rule already consumed.
    const record = golden.scans.find((entry) => entry.name === "a later rule matches further");
    const detector = new PatternDetector();
    const result = detector.scan(record?.event_type as string, record?.text as string, 7);
    expect(result.matchEnd).toBe(record?.match_end);
    expect(result.matchEnd).toBe((record?.text as string).length);
  });
});

describe("the description", () => {
  it("fills in the match and the event type", () => {
    expect(
      new PatternDetector([rule("found {match} in {event_type}")]).detect("read", "a secret-value", 1)[0]?.description,
    ).toBe(golden.templates.a_good_template);
  });

  it("never contains the match when the template is malformed", () => {
    // The match is the secret. A description that leaked it would flow
    // straight to telemetry and logs.
    for (const [template, expected] of [
      ["found {nonexistent}", golden.templates.a_missing_key],
      ["found {0}", golden.templates.a_positional_field],
    ] as const) {
      const description = new PatternDetector([rule(template)]).detect("read", "a secret-value", 1)[0]?.description;
      expect(description).toBe(expected);
      expect(description).not.toContain("secret-value");
    }
  });

  it("truncates a long match", () => {
    // An annotation is a marker, not a transcript.
    const description = new PatternDetector([rule("found {match}", /x+/)]).detect("read", "x".repeat(200), 1)[0]
      ?.description;
    expect(description).toBe(golden.templates.a_long_match_is_truncated);
    expect(description).toHaveLength(String(golden.templates.a_long_match_is_truncated).length);
  });

  it("truncates at the recorded width", () => {
    expect(String(golden.templates.a_long_match_is_truncated).replace("found ", "")).toHaveLength(
      golden.templates.truncate_at as number,
    );
  });
});

describe("streaming across chunks", () => {
  it.each(golden.streams)("$name", (record) => {
    // A detector that scans one chunk at a time misses a secret split across
    // two reads, which is exactly how a terminal delivers them.
    const streaming = new StreamingDetector(new PatternDetector());
    const produced = record.steps.map((step, index) => ({
      chunk: step.chunk,
      annotations: streaming.detect("read", step.chunk, index).map(annotationToWire),
    }));
    expect(produced).toStrictEqual(record.steps);
  });

  it("finds a key split down the middle", () => {
    const record = golden.streams.find((entry) => entry.name === "a key split down the middle");
    expect(record?.steps[0]?.annotations).toStrictEqual([]);
    expect(record?.steps[1]?.annotations).toHaveLength(1);
  });

  it("attributes the match to the chunk that completed it", () => {
    // The reviewer's timeline should point at the moment the secret finished
    // arriving, not at the one where it started.
    const record = golden.streams.find((entry) => entry.name === "a key split down the middle");
    expect(record?.steps[1]?.annotations[0]?.span).toStrictEqual({ from_seq: 1, to_seq: 1 });
  });

  it("does not report a completed match twice", () => {
    // The carry starts after the furthest match, so the next chunk does not
    // re-scan text that already produced an annotation.
    const record = golden.streams.find((entry) => entry.name === "a key wholly inside one chunk");
    expect(record?.steps[0]?.annotations).toHaveLength(1);
    expect(record?.steps[1]?.annotations).toStrictEqual([]);
  });

  it("still bridges a second secret starting right after the first", () => {
    const record = golden.streams.find((entry) => entry.name === "a second key right after the first");
    expect(record?.steps[0]?.annotations).toHaveLength(1);
    expect(record?.steps[1]?.annotations).toHaveLength(1);
  });

  it("bridges across three chunks", () => {
    const record = golden.streams.find((entry) => entry.name === "a match that completes on the third");
    expect(record?.steps.map((step) => step.annotations.length)).toStrictEqual([0, 0, 1]);
  });

  it("skips an empty chunk without losing the carry", () => {
    // An empty read happens; it must not throw the bridge away.
    const record = golden.streams.find((entry) => entry.name === "an empty chunk in the middle");
    expect(record?.steps.map((step) => step.annotations.length)).toStrictEqual([0, 0, 1]);
  });
});

describe("the carry", () => {
  it("produces nothing for an empty chunk", () => {
    const streaming = new StreamingDetector(new PatternDetector(), { maxCarry: 8 });
    streaming.detect("read", "0123456789abcdef", 0);
    expect(streaming.detect("read", "", 1)).toStrictEqual(golden.carry.an_empty_chunk_produces_nothing);
  });

  it("is bounded", () => {
    // The bound is what caps the memory held and the text re-scanned. A carry
    // too small to hold the start of the key cannot bridge it; one large
    // enough can, which is what proves the bound is doing the work.
    const tight = new StreamingDetector(new PatternDetector(), { maxCarry: 4 });
    tight.detect("read", "zzzzAKIAABCDEFGH", 0);
    expect(tight.detect("read", "IJKL", 1).map(annotationToWire)).toStrictEqual(
      golden.bounded_carry.too_small_to_bridge,
    );

    const roomy = new StreamingDetector(new PatternDetector(), { maxCarry: 64 });
    roomy.detect("read", "zzzzAKIAABCDEFGH", 0);
    expect(roomy.detect("read", "IJKL", 1).map(annotationToWire)).toStrictEqual(golden.bounded_carry.large_enough);
  });

  it("uses the recorded default bound", () => {
    expect(DEFAULT_MAX_CARRY).toBe(golden.carry.default_max_carry);
  });

  it("is forgotten on a reset", () => {
    // A screen clear or a resync means the next text does not continue the
    // last, and bridging across that would invent a match.
    const streaming = new StreamingDetector(new PatternDetector());
    streaming.detect("read", "AKIAABC", 0);
    streaming.reset();
    expect(streaming.detect("read", "DEFGHIJKL", 1).map(annotationToWire)).toStrictEqual(
      golden.carry.a_reset_forgets_the_tail,
    );
  });

  it("bridges when there is no reset", () => {
    const streaming = new StreamingDetector(new PatternDetector());
    streaming.detect("read", "AKIAABC", 0);
    expect(streaming.detect("read", "DEFGHIJKL", 1).map(annotationToWire)).toStrictEqual(
      golden.carry.without_a_reset_it_bridges,
    );
  });
});

describe("the wire form", () => {
  it("carries a null span when there is none", () => {
    // Always present, so a consumer need not tell absent from null.
    const annotation: Annotation = {
      label: "l",
      description: "d",
      severity: "s",
      source: "src",
      principal: "p",
    };
    expect(annotationToWire(annotation)).toStrictEqual(golden.empty_annotation);
  });

  it("carries the span when there is one", () => {
    const annotation: Annotation = {
      label: "l",
      description: "d",
      severity: "s",
      source: "src",
      principal: "p",
      span: { fromSeq: 1, toSeq: 2 },
    };
    expect(annotationToWire(annotation)).toStrictEqual(golden.annotation_with_span);
  });
});
