//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// biome-ignore-all lint/suspicious/noTemplateCurlyInString: `${step.path}` is the protocol's own grammar, and a
// test that wrote it as a template string would be testing this runtime's interpolation rather than the resolver's.

import { describe, expect, it } from "vitest";
import { resolveStep, type ScenarioStep } from "./index.ts";

/** What an earlier step recorded, in the shape a driver writes it down. */
function recorded(body: unknown, status: number | null = 200) {
  return new Map<string, unknown>([["acquire", { status, ok: true, body, error: null }]]);
}

/** A step with a field to resolve. */
function step(fields: Partial<ScenarioStep>): ScenarioStep {
  return { id: "send", action: "hijack_send", ...fields };
}

describe("a field that is a reference", () => {
  it("becomes what the step it names recorded", () => {
    // The one thing this whole module exists to do. A resolver whose pattern
    // never matches would leave the reference as written and every step would
    // ask a server about a literal `${...}` — which reads, in a matrix, as the
    // server refusing something.
    const resolved = resolveStep(step({ hijack_id: "${acquire.body.hijack_id}" }), recorded({ hijack_id: "h-1" }));

    expect(resolved.hijack_id).toBe("h-1");
  });

  it("reads a field of the record itself, not only of the body", () => {
    const resolved = resolveStep(step({ hijack_id: "${acquire.status}" }), recorded({}, 201));

    // Not every recorded field is a string, and a resolver that stringified
    // one would be shaping an observation on its way back out.
    expect<unknown>(resolved.hijack_id).toBe(201);
  });

  it("digs as deep as the path goes", () => {
    const resolved = resolveStep(step({ hijack_id: "${acquire.body.lease.id}" }), recorded({ lease: { id: "deep" } }));

    expect(resolved.hijack_id).toBe("deep");
  });

  it("indexes a list by number", () => {
    const resolved = resolveStep(
      step({ session_id: "${acquire.body.sessions.1.id}" }),
      recorded({ sessions: [{ id: "first" }, { id: "second" }] }),
    );

    expect(resolved.session_id).toBe("second");
  });

  it("resolves every field a step has, not just the first", () => {
    const resolved = resolveStep(
      step({ worker_id: "${acquire.body.worker}", hijack_id: "${acquire.body.hijack_id}" }),
      recorded({ worker: "w-1", hijack_id: "h-1" }),
    );

    expect(resolved).toStrictEqual({ id: "send", action: "hijack_send", worker_id: "w-1", hijack_id: "h-1" });
  });

  it("leaves the step it was given alone", () => {
    // The scenario is read once and may be reported; resolving in place would
    // rewrite what was asked for into what was sent.
    const original = step({ hijack_id: "${acquire.body.hijack_id}" });

    resolveStep(original, recorded({ hijack_id: "h-1" }));

    expect(original.hijack_id).toBe("${acquire.body.hijack_id}");
  });
});

describe("what is not a reference", () => {
  it.each([
    ["a reference with anything around it", "a${acquire.body.hijack_id}b"],
    ["a reference with a prefix", "id-${acquire.body.hijack_id}"],
    ["a reference with a suffix", "${acquire.body.hijack_id}!"],
    ["two references at once", "${acquire.status}${acquire.status}"],
    ["a step id the grammar does not allow", "${Acquire.body.hijack_id}"],
    ["a reference with no path at all", "${acquire}"],
    ["something merely shaped like one", "${ acquire.body }"],
    ["an ordinary string", "h-1"],
  ])("sends %s as written", (_name, value) => {
    // The grammar is deliberately the smallest thing that works: no
    // expressions, no nesting, no interpolation. Anything else is a value a
    // scenario meant literally.
    const resolved = resolveStep(step({ hijack_id: value }), recorded({ hijack_id: "h-1" }));

    expect(resolved.hijack_id).toBe(value);
  });

  it("leaves a field that is not a string alone", () => {
    const resolved = resolveStep(step({ lease_s: 30, keys: "x" }), recorded({ hijack_id: "h-1" }));

    expect(resolved.lease_s).toBe(30);
  });

  it("leaves a step with nothing to resolve exactly as it was", () => {
    const original = step({ worker_id: "w-1", hijack_id: "h-1" });

    expect(resolveStep(original, recorded({}))).toStrictEqual(original);
  });
});

describe("a reference that cannot be resolved", () => {
  it("refuses a step that has not run", () => {
    // A malformed scenario, not an observation: recording it as a field would
    // let the harness compare it as though the server had done something.
    expect(() => resolveStep(step({ hijack_id: "${nobody.body.hijack_id}" }), recorded({}))).toThrow(
      "which has not run",
    );
  });

  it("names the step and the reference it could not resolve", () => {
    expect(() => resolveStep(step({ hijack_id: "${nobody.body.hijack_id}" }), recorded({}))).toThrow(
      "step send: ${nobody.body.hijack_id}",
    );
  });

  it.each([
    ["a key that is not there", "${acquire.body.missing}", { hijack_id: "h-1" }],
    ["a key under one that is not there", "${acquire.body.lease.id}", { hijack_id: "h-1" }],
    ["an index past the end of a list", "${acquire.body.sessions.9}", { sessions: [{ id: "only" }] }],
    ["a name where a list wants an index", "${acquire.body.sessions.id}", { sessions: [{ id: "only" }] }],
    ["a path through a string", "${acquire.body.hijack_id.id}", { hijack_id: "h-1" }],
    ["a path through a null", "${acquire.body.nothing.id}", { nothing: null }],
    ["a field of the record that was never recorded", "${acquire.headers}", {}],
  ])("refuses %s", (_name, value, body) => {
    expect(() => resolveStep(step({ hijack_id: value }), recorded(body))).toThrow("is not there");
  });

  it("refuses a property nothing recorded, however a runtime spells it", () => {
    // `toString` is on every object in this language and on no recorded field.
    // A resolver reading it would answer a path the server never sent.
    expect(() => resolveStep(step({ hijack_id: "${acquire.body.toString}" }), recorded({}))).toThrow("is not there");
  });
});
