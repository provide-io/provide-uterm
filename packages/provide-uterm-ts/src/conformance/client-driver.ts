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

/** One step of a scenario, as `schema/scenario.schema.json` writes it. */
export interface ScenarioStep {
  id: string;
  action: string;
  auth?: AuthMode;
  path?: string;
  session_id?: string;
  body?: unknown;
  /**
   * Fields that differ legitimately between runs. Read by the harness, which
   * masks them before it compares; a driver records what it saw either way.
   */
  volatile?: string[];
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

/** Perform a scenario's steps in order and write down what came back. */
export async function runClientScenario(scenario: Scenario, options: ClientRunOptions): Promise<DriverResult> {
  const missing = (scenario.requires ?? []).filter((capability) => !CLIENT_CAPABILITIES.includes(capability));
  if (missing.length > 0) {
    // Not an error: the harness prints the cell as unsupported, and works out
    // what was missing from the capabilities this result carries.
    return result(options.scenarioId, "unsupported", [], null);
  }

  const steps: StepResult[] = [];
  for (const step of scenario.steps) {
    // One transport per step, because `auth` is per step and the transport's
    // record is of one request.
    const transport = new FetchTransport({
      baseUrl: options.baseUrl,
      token: options.token,
      auth: step.auth ?? "token",
      fetchImpl: options.fetchImpl,
    });
    const plan = planStep(new HijackClient({ transport }), step);
    if ("refuse" in plan) {
      // What ran already is still reported, so a reader can see how far it got.
      return result(options.scenarioId, "error", steps, plan.refuse);
    }
    steps.push({ id: step.id, fields: await observe(transport, plan.run) });
  }
  return result(options.scenarioId, "completed", steps, null);
}

/** Which call a step names, or why the driver cannot make it. */
function planStep(client: HijackClient, step: ScenarioStep): Plan {
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
      return { refuse: `step ${step.id}: unknown action ${step.action}` };
  }
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
