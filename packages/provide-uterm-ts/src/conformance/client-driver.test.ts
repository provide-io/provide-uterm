//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// biome-ignore-all lint/suspicious/noTemplateCurlyInString: `${step.path}` is the protocol's own grammar for a step
// that needs an earlier step's answer, and a scenario carries it as written.

import { describe, expect, it } from "vitest";
import {
  BAD_TOKEN,
  CLIENT_CAPABILITIES,
  type DriverResult,
  LANGUAGE,
  NON_JSON,
  runClientScenario,
  type Scenario,
  type ScenarioStep,
} from "./index.ts";

/** What the fake server was asked for. */
interface Asked {
  path: string;
  method: string;
  authorization: string | undefined;
  body: string | null;
}

/** An answer a fake server gives. */
interface Answer {
  status: number;
  body: string;
}

/** A fetch standing in for a server, answering by path. */
function server(answer: (path: string) => Answer) {
  const asked: Asked[] = [];
  const fetchImpl = (async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input));
    const headers = { ...(init?.headers as Record<string, string>) };
    asked.push({
      path: `${url.pathname}${url.search}`,
      method: init?.method ?? "GET",
      authorization: headers.Authorization,
      body: typeof init?.body === "string" ? init.body : null,
    });
    const { status, body } = answer(url.pathname);
    return new Response(body, { status });
  }) as unknown as typeof fetch;
  return { asked, fetchImpl };
}

/** A server that says yes to everything, naming the path it was asked for. */
function ok() {
  return server((path) => ({ status: 200, body: JSON.stringify({ path }) }));
}

/** Run one scenario against a fake server. */
async function run(steps: ScenarioStep[], fetchImpl: typeof fetch, requires?: string[]): Promise<DriverResult> {
  const scenario: Scenario = requires === undefined ? { steps } : { requires, steps };
  return runClientScenario(scenario, {
    scenarioId: "010_probe",
    baseUrl: "http://127.0.0.1:9",
    token: "issued",
    fetchImpl,
  });
}

describe("the shape of a result", () => {
  it("names the language, the role and the run", async () => {
    const fake = ok();

    const result = await run([{ id: "health", action: "health" }], fake.fetchImpl);

    expect(result).toStrictEqual({
      scenario_id: "010_probe",
      language: LANGUAGE,
      role: "client",
      status: "completed",
      capabilities: [...CLIENT_CAPABILITIES],
      steps: [{ id: "health", fields: { status: 200, ok: true, body: { path: "/api/health" }, error: null } }],
      error: null,
    });
    expect(LANGUAGE).toBe("typescript");
  });

  it("reports a server error as an observation, not as a failure", async () => {
    // The protocol is explicit: a step that got a 500 still completed. A
    // driver that called it a failure would be stating a verdict.
    const fake = server(() => ({ status: 500, body: '{"detail":"boom"}' }));

    const result = await run([{ id: "health", action: "health" }], fake.fetchImpl);

    expect(result.status).toBe("completed");
    expect(result.steps[0]?.fields).toStrictEqual({
      status: 500,
      ok: false,
      body: { detail: "boom" },
      error: null,
    });
  });

  it("tells one refusal from another underneath the client library", async () => {
    // `HijackClient` answers (ok, body) and drops the status, so a 401 and a
    // 404 would arrive as the same `ok: false`. The transport records what
    // came back before the library shapes it, and the driver claims
    // `status.observed` on the strength of that.
    const refusals = [401, 403, 404];
    const seen: Array<number | null> = [];
    for (const status of refusals) {
      const fake = server(() => ({ status, body: '{"detail":"no"}' }));
      const result = await run([{ id: "s", action: "list_sessions", auth: "bad" }], fake.fetchImpl);
      seen.push(result.steps[0]?.fields.status ?? null);
      expect(result.steps[0]?.fields.ok).toBe(false);
    }

    expect(seen).toStrictEqual(refusals);
    expect(CLIENT_CAPABILITIES).toContain("status.observed");
  });

  it("records a body nobody can parse as the same observation everywhere", async () => {
    const fake = server(() => ({ status: 200, body: "<html>up</html>" }));

    const result = await run([{ id: "health", action: "health" }], fake.fetchImpl);

    expect(result.steps[0]?.fields.body).toBe(NON_JSON);
    expect(NON_JSON).toBe("<non-json>");
  });

  it("records a server it could not reach with no status at all", async () => {
    const fetchImpl = (async () => {
      throw new Error("connect ECONNREFUSED 127.0.0.1:9");
    }) as unknown as typeof fetch;

    const result = await run([{ id: "health", action: "health" }], fetchImpl);

    expect(result.status).toBe("completed");
    expect(result.steps[0]?.fields).toStrictEqual({
      status: null,
      ok: false,
      body: null,
      error: "connect ECONNREFUSED 127.0.0.1:9",
    });
  });

  it("records a call the client library itself refused", async () => {
    // `../` in a session id would forge a route, and the client refuses to
    // build the path. That refusal is what this language does, so it is an
    // observation of the step rather than a driver fault.
    const fake = ok();

    const result = await run([{ id: "get", action: "get_session", session_id: "../admin" }], fake.fetchImpl);

    expect(result.status).toBe("completed");
    expect(fake.asked).toStrictEqual([]);
    expect(result.steps[0]?.fields.status).toBeNull();
    expect(result.steps[0]?.fields.ok).toBe(false);
    expect(result.steps[0]?.fields.error).toContain("invalid session_id");
  });
});

describe("the action vocabulary", () => {
  it("asks the client library for health", async () => {
    const fake = ok();

    await run([{ id: "s", action: "health" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/health", method: "GET" });
  });

  it("asks the client library for the session list", async () => {
    const fake = ok();

    await run([{ id: "s", action: "list_sessions" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/sessions", method: "GET" });
  });

  it("asks the client library for one session", async () => {
    const fake = ok();

    await run([{ id: "s", action: "get_session", session_id: "abc" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/sessions/abc", method: "GET" });
  });

  it("asks the client library for a session's snapshot", async () => {
    const fake = ok();

    await run([{ id: "s", action: "session_snapshot", session_id: "abc" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/sessions/abc/snapshot", method: "GET" });
  });

  it("gets a raw path", async () => {
    const fake = ok();

    await run([{ id: "s", action: "http_get", path: "/api/openapi.json" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/openapi.json", method: "GET" });
  });

  it("posts a raw path with a body", async () => {
    const fake = ok();

    await run([{ id: "s", action: "http_post", path: "/api/sessions", body: { rows: 24 } }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/sessions", method: "POST", body: '{"rows":24}' });
  });

  it("posts a raw path with no body when the scenario names none", async () => {
    const fake = ok();

    await run([{ id: "s", action: "http_post", path: "/api/sessions" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/sessions", method: "POST", body: null });
  });

  it("asks the client library for a session's events", async () => {
    const fake = ok();

    await run([{ id: "s", action: "session_events", session_id: "abc" }], fake.fetchImpl);

    // The reference driver's default, so a scenario that names no limit asks
    // both servers for the same number of events.
    expect(fake.asked[0]).toMatchObject({ path: "/api/sessions/abc/events?limit=100", method: "GET" });
  });

  it("asks for as many of a session's events as the step said", async () => {
    const fake = ok();

    await run([{ id: "s", action: "session_events", session_id: "abc", limit: 5 }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/api/sessions/abc/events?limit=5" });
  });

  it("asks the client library to put a session in a mode", async () => {
    const fake = ok();

    await run([{ id: "s", action: "set_input_mode", session_id: "abc", input_mode: "hijack" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({
      path: "/api/sessions/abc/mode",
      method: "POST",
      body: '{"input_mode":"hijack"}',
    });
  });

  it("takes a lease on a worker", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_acquire", worker_id: "w1" }], fake.fetchImpl);

    // The reference driver's defaults: whoever a scenario does not name is
    // `operator`, and a lease it does not size runs 90 seconds.
    expect(fake.asked[0]).toMatchObject({
      path: "/worker/w1/hijack/acquire",
      method: "POST",
      body: '{"owner":"operator","lease_s":90}',
    });
  });

  it("takes a lease for the owner and the time the step named", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_acquire", worker_id: "w1", owner: "ada", lease_s: 30 }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ body: '{"owner":"ada","lease_s":30}' });
  });

  it("extends a lease", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_heartbeat", worker_id: "w1", hijack_id: "h1" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({
      path: "/worker/w1/hijack/h1/heartbeat",
      method: "POST",
      body: '{"lease_s":90}',
    });
  });

  it("extends a lease for the time the step named", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_heartbeat", worker_id: "w1", hijack_id: "h1", lease_s: 15 }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ body: '{"lease_s":15}' });
  });

  it("sends keys through a lease", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_send", worker_id: "w1", hijack_id: "h1", keys: "ls\n" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/worker/w1/hijack/h1/send", method: "POST" });
    expect(JSON.parse(fake.asked[0]?.body ?? "null")).toMatchObject({ keys: "ls\n" });
  });

  it("sends nothing at all when a step names no keys", async () => {
    // The reference driver's default. Sending a key nobody asked for would be
    // a driver typing at a terminal on its own account.
    const fake = ok();

    await run([{ id: "s", action: "hijack_send", worker_id: "w1", hijack_id: "h1" }], fake.fetchImpl);

    expect(JSON.parse(fake.asked[0]?.body ?? "null")).toMatchObject({ keys: "" });
  });

  it("single-steps a hijacked worker", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_step", worker_id: "w1", hijack_id: "h1" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/worker/w1/hijack/h1/step", method: "POST" });
  });

  it("reads the screen through a lease", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_snapshot", worker_id: "w1", hijack_id: "h1" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/worker/w1/hijack/h1/snapshot?wait_ms=1500", method: "GET" });
  });

  it("gives a lease back", async () => {
    const fake = ok();

    await run([{ id: "s", action: "hijack_release", worker_id: "w1", hijack_id: "h1" }], fake.fetchImpl);

    expect(fake.asked[0]).toMatchObject({ path: "/worker/w1/hijack/h1/release", method: "POST" });
  });

  it("runs the steps in the order they are written", async () => {
    const fake = ok();

    const result = await run(
      [
        { id: "first", action: "health" },
        { id: "second", action: "list_sessions" },
      ],
      fake.fetchImpl,
    );

    expect(fake.asked.map((call) => call.path)).toStrictEqual(["/api/health", "/api/sessions"]);
    expect(result.steps.map((step) => step.id)).toStrictEqual(["first", "second"]);
  });
});

describe("what a step presents", () => {
  it("sends the server's token by default", async () => {
    const fake = ok();

    await run([{ id: "s", action: "health" }], fake.fetchImpl);

    expect(fake.asked[0]?.authorization).toBe("Bearer issued");
  });

  it("sends nothing for an unauthenticated step", async () => {
    const fake = ok();

    await run([{ id: "s", action: "health", auth: "none" }], fake.fetchImpl);

    expect(fake.asked[0]?.authorization).toBeUndefined();
  });

  it("sends a token no server issued for a bad-auth step", async () => {
    const fake = ok();

    await run([{ id: "s", action: "health", auth: "bad" }], fake.fetchImpl);

    expect(fake.asked[0]?.authorization).toBe(`Bearer ${BAD_TOKEN}`);
  });
});

describe("a scenario the driver cannot run", () => {
  it("stops on an action it does not know, and says which", async () => {
    // Never a silent skip: an unknown action means the driver is older than
    // the scenario, and a cell that quietly passed would hide that.
    const fake = ok();

    const result = await run(
      [
        { id: "first", action: "health" },
        { id: "second", action: "teleport" },
        { id: "third", action: "health" },
      ],
      fake.fetchImpl,
    );

    expect(result.status).toBe("error");
    expect(result.error).toContain("teleport");
    expect(result.error).toContain("second");
    // What did run is still reported, so a reader can see how far it got.
    expect(result.steps.map((step) => step.id)).toStrictEqual(["first"]);
  });

  it.each([
    ["get_session", { id: "s", action: "get_session" }, "session_id"],
    ["session_snapshot", { id: "s", action: "session_snapshot" }, "session_id"],
    ["session_events", { id: "s", action: "session_events" }, "session_id"],
    ["set_input_mode", { id: "s", action: "set_input_mode", input_mode: "open" }, "session_id"],
    ["set_input_mode", { id: "s", action: "set_input_mode", session_id: "abc" }, "input_mode"],
    ["hijack_acquire", { id: "s", action: "hijack_acquire" }, "worker_id"],
    ["hijack_heartbeat", { id: "s", action: "hijack_heartbeat", hijack_id: "h1" }, "worker_id"],
    ["hijack_heartbeat", { id: "s", action: "hijack_heartbeat", worker_id: "w1" }, "hijack_id"],
    ["hijack_send", { id: "s", action: "hijack_send", worker_id: "w1" }, "hijack_id"],
    ["hijack_step", { id: "s", action: "hijack_step", worker_id: "w1" }, "hijack_id"],
    ["hijack_snapshot", { id: "s", action: "hijack_snapshot", worker_id: "w1" }, "hijack_id"],
    ["hijack_release", { id: "s", action: "hijack_release", worker_id: "w1" }, "hijack_id"],
    ["http_get", { id: "s", action: "http_get" }, "path"],
    ["http_post", { id: "s", action: "http_post" }, "path"],
  ] as Array<[string, ScenarioStep, string]>)("stops when %s has no %s", async (_action, step, field) => {
    const fake = ok();

    const result = await run([step], fake.fetchImpl);

    expect(result.status).toBe("error");
    expect(result.error).toContain(field);
    expect(fake.asked).toStrictEqual([]);
  });
});

describe("a step that needs an earlier step's answer", () => {
  it("sends what the step it names came back with", async () => {
    // The one thing the harness cannot do for a driver: the driver performs
    // the request, so only the driver holds the value in time to use it.
    const fake = server((path) =>
      path.endsWith("/acquire")
        ? { status: 200, body: JSON.stringify({ hijack_id: "h-77" }) }
        : { status: 200, body: "{}" },
    );

    const result = await run(
      [
        { id: "acquire", action: "hijack_acquire", worker_id: "w1" },
        { id: "release", action: "hijack_release", worker_id: "w1", hijack_id: "${acquire.body.hijack_id}" },
      ],
      fake.fetchImpl,
    );

    expect(fake.asked[1]?.path).toBe("/worker/w1/hijack/h-77/release");
    expect(result.status).toBe("completed");
    expect(result.steps.map((step) => step.id)).toStrictEqual(["acquire", "release"]);
  });

  it("resolves against what was recorded, not against what the library concluded", async () => {
    // The record holds the status the library drops, so a scenario may refer
    // to it — and a driver that resolved against `(ok, body)` could not.
    const fake = server(() => ({ status: 200, body: JSON.stringify({ input_mode: "hijack" }) }));

    await run(
      [
        { id: "first", action: "list_sessions" },
        { id: "second", action: "set_input_mode", session_id: "abc", input_mode: "${first.body.input_mode}" },
      ],
      fake.fetchImpl,
    );

    expect(fake.asked[1]?.body).toBe('{"input_mode":"hijack"}');
  });

  it("stops on a reference to a step that has not run", async () => {
    // A malformed scenario, and a run error rather than a step observation:
    // recording it as a field would let the harness compare it as though the
    // server had done something.
    const fake = ok();

    const result = await run(
      [
        { id: "first", action: "health" },
        { id: "second", action: "hijack_release", worker_id: "w1", hijack_id: "${nobody.body.hijack_id}" },
      ],
      fake.fetchImpl,
    );

    expect(result.status).toBe("error");
    expect(result.error).toContain("has not run");
    expect(result.steps.map((step) => step.id)).toStrictEqual(["first"]);
    expect(fake.asked.map((call) => call.path)).toStrictEqual(["/api/health"]);
  });

  it("stops on a reference to something the step it names never recorded", async () => {
    const fake = ok();

    const result = await run(
      [
        { id: "first", action: "health" },
        { id: "second", action: "hijack_release", worker_id: "w1", hijack_id: "${first.body.hijack_id}" },
      ],
      fake.fetchImpl,
    );

    expect(result.status).toBe("error");
    expect(result.error).toContain("is not there");
    expect(result.steps).toHaveLength(1);
  });
});

describe("capabilities", () => {
  it("runs a scenario that requires nothing", async () => {
    const fake = ok();

    const result = await run([{ id: "s", action: "health" }], fake.fetchImpl, []);

    expect(result.status).toBe("completed");
  });

  it("runs a scenario requiring only what this driver has", async () => {
    const fake = ok();

    const result = await run([{ id: "s", action: "health" }], fake.fetchImpl, [...CLIENT_CAPABILITIES]);

    expect(result.status).toBe("completed");
    expect(CLIENT_CAPABILITIES.length).toBeGreaterThan(0);
  });

  it("reports a missing capability as unsupported rather than running anyway", async () => {
    const fake = ok();

    const result = await run([{ id: "s", action: "health" }], fake.fetchImpl, ["rfb.raw"]);

    expect(result.status).toBe("unsupported");
    expect(result.steps).toStrictEqual([]);
    expect(result.error).toBeNull();
    // The harness works out what was missing from what the driver has.
    expect(result.capabilities).toStrictEqual([...CLIENT_CAPABILITIES]);
    expect(fake.asked).toStrictEqual([]);
  });
});
