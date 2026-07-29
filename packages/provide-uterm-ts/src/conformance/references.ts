//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A step that needs an earlier step's answer.
 *
 * `hijack_send` needs the `hijack_id` that `hijack_acquire` came back with, so
 * a string field in a step may be a **reference**:
 *
 * ```json
 * { "id": "send", "action": "hijack_send", "hijack_id": "${acquire.body.hijack_id}" }
 * ```
 *
 * `conformance/live/PROTOCOL.md` is explicit that this is the one place the
 * "drivers observe, the harness judges" rule does not reach: the driver builds
 * the request, so the driver is the only thing holding the value in time to
 * use it. The harness cannot resolve what it never saw.
 *
 * The grammar is the smallest thing that works — one step id, one dotted path,
 * no expressions, no defaults, no nesting, and the **whole field** must be the
 * reference, so `"a${x.y}b"` is a value a scenario meant literally. Four
 * implementations of one small thing is four chances to disagree, and every
 * rule left implicit is one of them.
 *
 * Its own module because it is the seam the protocol says to expect trouble
 * on: a resolver whose pattern never matched would leave every reference as
 * written, and each step would ask a server about a literal `${...}` — which
 * reads, in a matrix, as the server having refused something.
 */

import type { ScenarioStep } from "./client-driver.ts";

/**
 * The one shape a reference has.
 *
 * A regular-expression *literal*, and not a pattern built from a string: a
 * `\\$` in a string is a literal backslash, and a resolver spelt that way
 * matches nothing at all while looking entirely correct.
 */
export const REFERENCE = /^\$\{([a-z0-9_]+)\.([A-Za-z0-9_.]+)\}$/;

/** What a segment of a list index may be made of. */
const INDEX = /^[0-9]+$/;

/** Nothing was there — which a recorded `null` is not. */
const ABSENT = Symbol("absent");

/**
 * Read a dotted path out of what a step recorded.
 *
 * Objects by key and lists by index, and nothing else: a path that runs off
 * the end of either is absent rather than undefined, because a step may
 * legitimately have recorded a null and the two must not read the same.
 *
 * Only a key the record actually has counts. Every object in this runtime
 * carries `toString` and its neighbours, and a resolver that answered one of
 * them would hand a scenario a value no server ever sent.
 */
function dig(node: unknown, segments: readonly string[]): unknown {
  let found = node;
  for (const segment of segments) {
    if (Array.isArray(found)) {
      if (!INDEX.test(segment) || Number(segment) >= found.length) {
        return ABSENT;
      }
      found = found[Number(segment)];
    } else if (typeof found === "object" && found !== null && Object.hasOwn(found, segment)) {
      found = (found as Record<string, unknown>)[segment];
    } else {
      return ABSENT;
    }
  }
  return found;
}

/**
 * The step with every reference replaced by what the step it names recorded.
 *
 * @param step The step as the scenario wrote it. Left untouched.
 * @param seen What each step that has already run recorded, by step id.
 * @throws {Error} When a reference names a step that has not run, or a path
 *   that is not there. That is a malformed scenario rather than something a
 *   server did, so it ends the run: recording it as a field would let the
 *   harness compare it as though the server had answered.
 * @returns A step whose fields are what the requests should be built from.
 */
export function resolveStep(step: ScenarioStep, seen: ReadonlyMap<string, unknown>): ScenarioStep {
  const resolved: ScenarioStep = { ...step };
  // A view of the copy: what a reference resolves to is whatever the earlier
  // step recorded, and the step's own field types describe what a scenario
  // wrote rather than what came back.
  const fields = resolved as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(fields)) {
    if (typeof value !== "string") {
      continue;
    }
    const match = REFERENCE.exec(value);
    if (match === null) {
      continue;
    }
    const named = match[1] as string;
    const path = match[2] as string;
    if (!seen.has(named)) {
      throw new Error(`step ${step.id}: ${value} names ${named}, which has not run`);
    }
    const found = dig(seen.get(named), path.split("."));
    if (found === ABSENT) {
      throw new Error(`step ${step.id}: ${value} is not there`);
    }
    fields[key] = found;
  }
  return resolved;
}
