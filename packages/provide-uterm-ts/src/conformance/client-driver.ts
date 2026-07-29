//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Running one scenario in the client role.
 *
 * The rule this file is written around is the one in
 * `conformance/live/PROTOCOL.md`: **drivers observe, the harness judges.** No
 * expectation is read here and no verdict is reached. A step is performed, and
 * what came back is written down.
 *
 * So `status` describes the run rather than the outcome:
 *
 * * `completed` — every step ran. A 500, a refusal, even a server that could
 *   not be reached are observations, and the harness decides what they mean.
 * * `unsupported` — the scenario needs a capability this language does not
 *   report, so nothing was run.
 * * `error` — the driver could not perform a step at all: an action it does
 *   not know, or a step missing what that action needs. Never a silent skip,
 *   because a quietly-passing cell is how a matrix comes to mean nothing.
 */

import { type HijackAnswer, HijackClient } from "../client/hijack-client.ts";
import { resolveStep } from "./references.ts";
import { type AuthMode, errorMessage, FetchTransport } from "./transport.ts";

/** What this driver calls itself in a result. */
export const LANGUAGE = "typescript";

/** What a body that is not JSON is recorded as, in every language. */
export const NON_JSON = "<non-json>";

/**
 * What this driver can do, as the protocol's capability names.
 *
 * `status.observed` is claimed because `HijackClient` takes a transport: the
 * library performs each call, and the transport underneath it records the
 * status the library drops. A language that could not inject one would have
 * to record `status: null` and leave this capability out, so that the gap
 * showed up in the matrix rather than hiding in it.
 */
export const CLIENT_CAPABILITIES: readonly string[] = ["hijack.rest", "status.observed"];

/**
 * One step of a scenario, as `schema/scenario.schema.json` writes it.
 *
 * A string field may be a reference to what an earlier step recorded — see
 * {@link resolveStep} — so what a scenario wrote and what a request is built
 * from are not always the same value.
 */
export interface ScenarioStep {
  id: string;
  action: string;
  auth?: AuthMode;
  path?: string;
  session_id?: string;
  body?: unknown;
  /** The worker whose lease a hijack action acts on. */
  worker_id?: string;
  /** The lease itself. Normally a reference to the acquiring step. */
  hijack_id?: string;
  /** Who is taking a lease. */
  owner?: string;
  /** How long a lease runs. */
  lease_s?: number;
  /** What `hijack_send` types. */
  keys?: string;
  /** `open` or `hijack`, in the reference's own vocabulary. */
  input_mode?: string;
  /** How many events `session_events` reads. */
  limit?: number;
  /**
   * Fields that differ legitimately between runs. Read by the harness, which
   * masks them before it compares; a driver records what it saw either way.
   */
  volatile?: string[];
  /**
   * Do this step this many times — see {@link observationIds}.
   *
   * Not an action field: it changes how many times the step is performed and
   * nothing about what is sent.
   */
  repeat?: number;
}

/** A scenario, as far as a client driver reads one. */
export interface Scenario {
  id?: string;
  requires?: string[];
  steps: ScenarioStep[];
}

/** What was observed of one step. */
export interface StepFields {
  status: number | null;
  ok: boolean;
  body: unknown;
  error: string | null;
}

/** One step's record. */
export interface StepResult {
  id: string;
  fields: StepFields;
}

/** What a driver writes to stdout, as `schema/result.schema.json` requires. */
export interface DriverResult {
  scenario_id: string;
  language: string;
  role: "client" | "server";
  status: "completed" | "unsupported" | "error";
  capabilities: string[];
  steps: StepResult[];
  error: string | null;
}

/** What a client run is pointed at. */
export interface ClientRunOptions {
  scenarioId: string;
  baseUrl: string;
  token: string;
  fetchImpl?: typeof fetch | undefined;
}

/** A step the driver can perform, or the reason it cannot. */
type Plan = { run: () => Promise<HijackAnswer> } | { refuse: string };

/** The actions that act on a lease somebody already holds. */
const THROUGH_A_LEASE = new Set([
  "hijack_heartbeat",
  "hijack_send",
  "hijack_step",
  "hijack_snapshot",
  "hijack_release",
]);

/** Perform a scenario's steps in order and write down what came back. */
export async function runClientScenario(scenario: Scenario, options: ClientRunOptions): Promise<DriverResult> {
  const missing = (scenario.requires ?? []).filter((capability) => !CLIENT_CAPABILITIES.includes(capability));
  if (missing.length > 0) {
    // Not an error: the harness prints the cell as unsupported, and works out
    // what was missing from the capabilities this result carries.
    return result(options.scenarioId, "unsupported", [], null);
  }

  const steps: StepResult[] = [];
  // What each step recorded, for the steps that refer to each other's answers.
  const seen = new Map<string, StepFields>();
  for (const written of scenario.steps) {
    let step: ScenarioStep;
    try {
      step = resolveStep(written, seen);
    } catch (error) {
      // A reference nobody can resolve is a malformed scenario, not something
      // a server did. Recording it as a field would let the harness compare it
      // as though the server had answered.
      return result(options.scenarioId, "error", steps, errorMessage(error));
    }

    // Resolved once, before the repetitions, as the reference driver does: a
    // reference can never name a repeated step, so there is nothing a second
    // resolution could see that the first did not.
    for (const observed of observationIds(step.id, written.repeat)) {
      // One transport per request, because `auth` is per step and the
      // transport's record is of one request — so a repeated step needs one
      // apiece rather than a record of only its last repetition.
      const transport = new FetchTransport({
        baseUrl: options.baseUrl,
        token: options.token,
        auth: step.auth ?? "token",
        fetchImpl: options.fetchImpl,
      });
      const plan = planStep(new HijackClient({ transport }), step);
      if ("refuse" in plan) {
        // What ran already is still reported, so a reader can see how far it
        // got.
        return result(options.scenarioId, "error", steps, plan.refuse);
      }
      // Awaited in turn, never started together: a scenario repeats a step to
      // watch a budget being spent, and answers that raced would say a request
      // was refused without saying which one.
      const fields = await observe(transport, plan.run);
      seen.set(observed, fields);
      steps.push({ id: observed, fields });
    }
  }
  return result(options.scenarioId, "completed", steps, null);
}

/**
 * The ids one step's observations are recorded under.
 *
 * A step that runs once keeps its own id; a repeated step numbers its
 * repetitions from zero — `flood.0`, `flood.1` — and the bare id records
 * nothing. Every repetition is recorded, never just the last: a scenario
 * repeats a step precisely because it expects the answers to stop being the
 * same, so a driver that kept only the final answer would turn "the
 * thirty-first request was refused" into "a request was refused", and those
 * are different claims about a budget.
 */
function observationIds(stepId: string, repeat: number | undefined): string[] {
  // There is no `repeat: 1`. Two ways of writing the same thing, where one of
  // them renumbers every observation, is a difference nobody would remember
  // when reading a scenario — so the harness refuses it and one run is one
  // bare id either way.
  const times = repeat ?? 1;
  if (times === 1) {
    return [stepId];
  }
  return Array.from({ length: times }, (_unused, index) => `${stepId}.${index}`);
}

/**
 * Which call a step names, or why the driver cannot make it.
 *
 * Every one of them goes through `HijackClient`, because what is under test is
 * the client library a consumer would actually use — a hand-rolled request
 * that happened to agree with it would prove only that the driver and the
 * server agree.
 */
function planStep(client: HijackClient, step: ScenarioStep): Plan {
  return (
    planSession(client, step) ??
    planHijack(client, step) ?? { refuse: `step ${step.id}: unknown action ${step.action}` }
  );
}

/** The sessions half of the vocabulary, or null if the step is not one. */
function planSession(client: HijackClient, step: ScenarioStep): Plan | null {
  switch (step.action) {
    case "health":
      return { run: () => client.health() };
    case "list_sessions":
      return { run: () => client.listSessions() };
    case "get_session": {
      const sessionId = step.session_id;
      return sessionId === undefined
        ? { refuse: refusal(step, "session_id") }
        : { run: () => client.getSession(sessionId) };
    }
    case "session_snapshot": {
      const sessionId = step.session_id;
      return sessionId === undefined
        ? { refuse: refusal(step, "session_id") }
        : { run: () => client.sessionSnapshot(sessionId) };
    }
    case "session_events": {
      const sessionId = step.session_id;
      // A limit nobody set is the library's own default, which is the
      // reference client's: a driver holding a second copy of that number is a
      // second place for it to drift.
      return sessionId === undefined
        ? { refuse: refusal(step, "session_id") }
        : { run: () => client.sessionEvents(sessionId, { limit: step.limit }) };
    }
    case "set_input_mode": {
      const sessionId = step.session_id;
      const mode = step.input_mode;
      if (sessionId === undefined) {
        return { refuse: refusal(step, "session_id") };
      }
      // A mode nobody named has no sensible default: `open` and `hijack` are
      // opposite instructions, and guessing one would put a session into a
      // state the scenario never asked for.
      return mode === undefined
        ? { refuse: refusal(step, "input_mode") }
        : { run: () => client.setSessionMode(sessionId, mode) };
    }
    // The raw pair reaches the surfaces no client method covers. They go
    // through the library's own request primitive: one method, one path and
    // nothing built on top, which is what "a raw GET of `path`" asks for.
    case "http_get": {
      const path = step.path;
      return path === undefined ? { refuse: refusal(step, "path") } : { run: () => client.request("GET", path) };
    }
    case "http_post": {
      const path = step.path;
      return path === undefined
        ? { refuse: refusal(step, "path") }
        : { run: () => client.request("POST", path, { json: step.body }) };
    }
    default:
      return null;
  }
}

/** The hijack half of the vocabulary, or null if the step is not one. */
function planHijack(client: HijackClient, step: ScenarioStep): Plan | null {
  if (step.action === "hijack_acquire") {
    const workerId = step.worker_id;
    // An owner and a lease nobody set are the library's own defaults —
    // `operator` and ninety seconds — which are the reference driver's.
    return workerId === undefined
      ? { refuse: refusal(step, "worker_id") }
      : { run: () => client.acquire(workerId, { owner: step.owner, leaseS: step.lease_s }) };
  }

  const held = lease(step);
  if (held === null) {
    return null;
  }
  if ("refuse" in held) {
    return held;
  }
  const { workerId, hijackId } = held;
  switch (step.action) {
    case "hijack_heartbeat":
      return { run: () => client.heartbeat(workerId, hijackId, { leaseS: step.lease_s }) };
    case "hijack_send":
      // Nothing is what a step that names no keys sends, as the reference
      // driver does: a driver inventing one would be typing at a terminal on
      // its own account.
      return { run: () => client.send(workerId, hijackId, { keys: step.keys ?? "" }) };
    case "hijack_step":
      return { run: () => client.step(workerId, hijackId) };
    case "hijack_snapshot":
      return { run: () => client.snapshot(workerId, hijackId) };
    // `hijack_release`, and only it: the set in `lease` already settled which
    // actions reach here, so a case naming it again would be a branch no
    // scenario could take.
    default:
      return { run: () => client.release(workerId, hijackId) };
  }
}

/**
 * The worker and the lease a hijack action acts on.
 *
 * Null for a step that is not one of them, and a refusal for a step that is
 * but does not say which lease: a hijack action without one has no route to
 * ask for, and inventing an identifier would ask a server about a lease
 * nobody holds.
 */
function lease(step: ScenarioStep): { workerId: string; hijackId: string } | { refuse: string } | null {
  if (!THROUGH_A_LEASE.has(step.action)) {
    return null;
  }
  const workerId = step.worker_id;
  if (workerId === undefined) {
    return { refuse: refusal(step, "worker_id") };
  }
  const hijackId = step.hijack_id;
  return hijackId === undefined ? { refuse: refusal(step, "hijack_id") } : { workerId, hijackId };
}

/** Why a step cannot be performed as written. */
function refusal(step: ScenarioStep, field: string): string {
  return `step ${step.id}: ${step.action} needs ${field}`;
}

/**
 * Make the call and write down what came back.
 *
 * `ok` and `body` are the library's own shaping, untouched. `status` comes
 * from underneath it, and `error` says why there is no status when there is
 * none — a server that was never reached, or a call the library itself
 * refused to make.
 */
async function observe(transport: FetchTransport, run: () => Promise<HijackAnswer>): Promise<StepFields> {
  let answer: HijackAnswer;
  try {
    answer = await run();
  } catch (error) {
    // The library refused before it called: an identifier it would not put in
    // a path. That refusal is this language's behaviour, so it is an
    // observation of the step and not a fault of the driver.
    return { status: transport.attempt.status, ok: false, body: null, error: errorMessage(error) };
  }

  if (transport.attempt.error !== null) {
    // Nothing answered, so there is no status and no body to record — only
    // why. A null here is the observation, not a gap in it.
    return { status: null, ok: false, body: null, error: transport.attempt.error };
  }
  return {
    status: transport.attempt.status,
    ok: answer.ok,
    body: transport.attempt.jsonOk ? answer.body : NON_JSON,
    error: null,
  };
}

/** A result in the shape the schema requires. */
function result(
  scenarioId: string,
  status: DriverResult["status"],
  steps: StepResult[],
  error: string | null,
): DriverResult {
  return {
    scenario_id: scenarioId,
    language: LANGUAGE,
    role: "client",
    status,
    capabilities: [...CLIENT_CAPABILITIES],
    steps,
    error,
  };
}
