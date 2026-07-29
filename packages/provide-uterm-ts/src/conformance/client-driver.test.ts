//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

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
