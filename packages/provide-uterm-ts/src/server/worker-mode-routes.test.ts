//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The worker's own mode route: `POST /worker/{id}/input_mode`.
 *
 * Port of `provide.uterm.server.bridge.routes.rest_workerctl`, gated by
 * `provide.uterm.server.app.hub_authz` — a sibling of the lease routes rather
 * than of the session ones, which is why it is served from `hijack-routes.ts`
 * behind the same gate.
 *
 * The whole point of the route is the one thing it will not do. There are two
 * ways onto a session's input mode and they are deliberately unequal:
 *
 * | route | while a lease is held |
 * |---|---|
 * | `POST /worker/{id}/input_mode` | refused 409, lease untouched |
 * | `POST /api/sessions/{id}/mode` | 200, lease force-released |
 *
 * The difference is authority. An operator opening a session is entitled to
 * take it back from whoever holds the lease — that is the arbitration this
 * product is for. A worker is not: `open` means everyone may type, so letting
 * a worker flip it would end the holder's exclusivity while their lease still
 * answered heartbeats, telling them they hold something they do not.
 *
 * The corpus in `hijack-routes.test.ts` holds the answers themselves against
 * the reference. What is here is the *consequence* — that a refused mode
 * change leaves a working lease behind, which no single response body can
 * show.
 */

import { describe, expect, it } from "vitest";
import { bootstrapServer } from "./bootstrap.ts";
import { handleHijackRequest, WORKER_MODE_ACTIVE_HIJACK_ERROR, WORKER_MODE_NO_WORKER_ERROR } from "./hijack-routes.ts";
import { SessionHub } from "./session-hub.ts";

/** The base a `Request` is built against. Never reaches the wire. */
const BASE = "http://127.0.0.1:0";

/** The session the default configuration defines, and the worker it becomes. */
const SESSION = "provide-shell";

/** A server with its configured session started and attached to the hub. */
async function running() {
  const bootstrapped = bootstrapServer({ authMode: "dev_token", now: () => 1_700_000_000 });
  await bootstrapped.runtimes.startAutoStart();
  return bootstrapped;
}

/** One request to the application, as an authenticated caller. */
function ask(
  app: { handle(request: Request): Promise<Response> },
  token: string,
  method: string,
  path: string,
  body?: unknown,
): Promise<Response> {
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  if (body === undefined) {
    return app.handle(new Request(`${BASE}${path}`, { method, headers }));
  }
  return app.handle(
    new Request(`${BASE}${path}`, {
      method,
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

/** Put the session in `hijack` mode and take a lease on it. */
async function held(app: { handle(request: Request): Promise<Response> }, token: string): Promise<string> {
  await ask(app, token, "POST", `/api/sessions/${SESSION}/mode`, { input_mode: "hijack" });
  const acquired = await ask(app, token, "POST", `/worker/${SESSION}/hijack/acquire`, {
    owner: "holder",
    lease_s: 60,
  });
  expect(acquired.status).toBe(200);
  return ((await acquired.json()) as { hijack_id: string }).hijack_id;
}

describe("a worker asking to open a session somebody holds", () => {
  it("refuses, and leaves the lease working", async () => {
    const { app, token } = await running();
    const hijackId = await held(app, token);

    const refused = await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "open" });
    expect(refused.status).toBe(409);
    expect(await refused.json()).toEqual({ error: WORKER_MODE_ACTIVE_HIJACK_ERROR });

    // The refusal refused; it did not disturb what it refused on behalf of. A
    // guard that answered 409 and wrote the field anyway would pass the line
    // above and fail both of these.
    const beat = await ask(app, token, "POST", `/worker/${SESSION}/hijack/${hijackId}/heartbeat`, { lease_s: 60 });
    expect(beat.status).toBe(200);
    const rival = await ask(app, token, "POST", `/worker/${SESSION}/hijack/acquire`, { owner: "rival", lease_s: 60 });
    expect(rival.status).toBe(409);
    expect(await rival.json()).toEqual({ error: "Worker is already hijacked." });
  });

  it("is the only one of the two routes that is refused", async () => {
    const { app, token } = await running();
    const hijackId = await held(app, token);
    expect((await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "open" })).status).toBe(409);

    // The same field, through the route an operator owns. This one takes the
    // session back rather than being refused, and the lease is gone rather
    // than merely bypassed — a lease left alive while everyone could type
    // would tell its holder they still had something they did not.
    const opened = await ask(app, token, "POST", `/api/sessions/${SESSION}/mode`, { input_mode: "open" });
    expect(opened.status).toBe(200);
    expect((await opened.json()) as { input_mode: string }).toMatchObject({ input_mode: "open" });
    const beat = await ask(app, token, "POST", `/worker/${SESSION}/hijack/${hijackId}/heartbeat`, { lease_s: 60 });
    expect(beat.status).toBe(404);
  });

  it("still re-asserts the mode it is already in", async () => {
    const { app, token } = await running();
    const hijackId = await held(app, token);
    // A worker that reconnects re-sends the mode it was configured with. The
    // guard is about *opening* a held session, not about writing the field, so
    // `hijack` while hijacked is a no-op and not a refusal — a port that
    // refused it would break every reconnect into a held session.
    const again = await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "hijack" });
    expect(again.status).toBe(200);
    expect(await again.json()).toEqual({ ok: true, input_mode: "hijack", worker_id: SESSION });
    expect((await ask(app, token, "POST", `/worker/${SESSION}/hijack/${hijackId}/heartbeat`, {})).status).toBe(200);
  });
});

describe("a worker asking for a mode nobody is holding against it", () => {
  it("answers the shape a client parses, and moves the field", async () => {
    const { app, token, hub } = await running();
    const opened = await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "open" });
    expect(opened.status).toBe(200);
    expect(await opened.json()).toEqual({ ok: true, input_mode: "open", worker_id: SESSION });
    expect(hub.registry.get(SESSION)?.inputMode).toBe("open");

    // And back, which is the transition the guard never looks at.
    const closed = await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "hijack" });
    expect(closed.status).toBe(200);
    expect(hub.registry.get(SESSION)?.inputMode).toBe("hijack");
  });

  it("takes the mode it is already in, either way round", async () => {
    const { app, token } = await running();
    await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "open" });
    expect((await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "open" })).status).toBe(200);
  });
});

describe("what the mode route refuses before it reaches the hub", () => {
  it("asks for a credential first, and tells an anonymous caller nothing else", async () => {
    const { app } = await running();
    const anonymous = await app.handle(
      new Request(`${BASE}/worker/${SESSION}/input_mode`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ input_mode: "open" }),
      }),
    );
    expect(anonymous.status).toBe(401);
    expect(await anonymous.json()).toEqual({ detail: "authentication required" });
  });

  it("calls the thing a session when it cannot find one", async () => {
    const { app, token } = await running();
    const unknown = await ask(app, token, "POST", "/worker/no-such-worker/input_mode", { input_mode: "open" });
    expect(unknown.status).toBe(404);
    expect(await unknown.json()).toEqual({ detail: "unknown session: no-such-worker" });
  });

  it("says the worker is not registered when the session exists and the worker does not", async () => {
    // The gate looks in the session registry and the hub looks in its own
    // worker table, and they are not the same table. A configured session
    // whose connector never came up passes the first and fails the second,
    // which is the only way to reach the hub's own 404 from outside.
    const { app, token } = bootstrapServer({ authMode: "dev_token", now: () => 1_700_000_000 });
    const unattached = await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "hijack" });
    expect(unattached.status).toBe(404);
    expect(await unattached.json()).toEqual({ error: WORKER_MODE_NO_WORKER_ERROR });
  });

  it("refuses a caller who may read the session but not steer it", async () => {
    const hub = new SessionHub({ wallNow: () => 1_700_000_000 });
    hub.registerWorker(SESSION, { sendText: async () => {} }, "hijack");
    const { registry } = bootstrapServer({ authMode: "jwt" });
    const answer = await handleHijackRequest(
      new Request(`${BASE}/worker/${SESSION}/input_mode`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ input_mode: "open" }),
      }),
      {
        hub,
        registry,
        principal: { subject_id: "someone", roles: new Set(["viewer"]), scopes: new Set(["*"]) },
        authenticated: true,
      },
    );
    // `session.control.mode` is an operator's, not a viewer's — and it is not
    // the lease routes' `session.control.hijack` either, which an operator
    // does not hold.
    expect(answer?.status).toBe(403);
    expect(await answer?.json()).toEqual({ detail: "insufficient privileges" });
  });

  it("refuses a mode nobody defined, and does not tidy one up on the way in", async () => {
    const { app, token, hub } = await running();
    expect((await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: "hijack" })).status).toBe(200);
    for (const value of ["sideways", "", " open", "OPEN", 1, null]) {
      const refused = await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, { input_mode: value });
      expect(`${JSON.stringify(value)}: ${refused.status}`).toBe(`${JSON.stringify(value)}: 422`);
    }
    // No body at all, and a body with the field missing, are the same refusal.
    expect((await ask(app, token, "POST", `/worker/${SESSION}/input_mode`, {})).status).toBe(422);
    expect((await ask(app, token, "POST", `/worker/${SESSION}/input_mode`)).status).toBe(422);
    // Nothing above moved the field. `" open"` is the one worth saying out
    // loud: the *session* route strips its input and this one does not, so a
    // caller that pads its payload is refused here and obeyed there.
    expect(hub.registry.get(SESSION)?.inputMode).toBe("hijack");
  });

  it("reads the path before anything else, and the method before the credential", async () => {
    const { app, token } = await running();
    const malformed = await ask(app, token, "POST", "/worker/not%20a%20worker/input_mode", { input_mode: "open" });
    expect(malformed.status).toBe(422);
    const wrongMethod = await ask(app, token, "GET", `/worker/${SESSION}/input_mode`);
    expect(wrongMethod.status).toBe(405);
    expect(await wrongMethod.json()).toEqual({ detail: "Method Not Allowed" });
    expect(wrongMethod.headers.get("allow")).toBe("POST");
  });

  it("leaves every other worker path to whoever else claims it", async () => {
    const { app, token } = await running();
    // Not a verb this port serves. `disconnect_worker` is the reference's
    // other worker-control route and is not ported, so it is a 404 here rather
    // than a route that answers something approximate.
    expect((await ask(app, token, "POST", `/worker/${SESSION}/disconnect_worker`, {})).status).toBe(404);
    expect((await ask(app, token, "POST", `/worker/${SESSION}/hijack`, {})).status).toBe(404);
    expect((await ask(app, token, "POST", `/worker/${SESSION}`, {})).status).toBe(404);
    expect((await ask(app, token, "POST", `/worker/${SESSION}/input_mode/extra`, {})).status).toBe(404);
  });
});
