//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The lease and snapshot routes, held to `serverhijack_golden`.
 *
 * The corpus is a *sequence* recorded off the running reference server, not a
 * set of independent decisions: each answer depends on what the ones before it
 * did, and a later probe may quote an earlier one's hijack id. So it is
 * replayed in order against one server, exactly as it was recorded.
 *
 * That is the whole of the parity evidence for these routes. Everything below
 * it tests a branch the reference cannot be made to take from outside — a
 * worker that has gone, a caller who holds less than an administrator's token,
 * a guard that will not compile.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { encodeJwt } from "../serverauth/index.ts";
import { bootstrapServer } from "./bootstrap.ts";
import { handleHijackRequest, parseHijackPath } from "./hijack-routes.ts";
import { SessionHub } from "./session-hub.ts";

interface Probe {
  id: string;
  method: string;
  path: string;
  auth: string;
  status: number;
  headers: Record<string, string>;
  body: unknown;
  body_keys?: string[];
  json?: Record<string, unknown>;
}

const CORPUS = JSON.parse(
  readFileSync(join(import.meta.dirname, "..", "..", "testdata", "serverhijack_golden.json"), "utf8"),
) as { volatile: string; session_id: string; probes: Probe[] };

/** Which paths of which probe's body differ between two runs of the same server. */
const VOLATILE_PATHS: Readonly<Record<string, readonly string[]>> = {
  snapshot_before_hijack: ["*"],
  to_hijack: ["created_at", "connected", "lifecycle_state"],
  acquire: ["hijack_id", "lease_expires_at"],
  heartbeat: ["hijack_id", "lease_expires_at"],
  hijack_snapshot: ["*"],
  hijack_send: ["hijack_id", "lease_expires_at"],
  hijack_step: ["hijack_id", "lease_expires_at"],
  session_snapshot_while_held: ["*"],
  release: ["hijack_id"],
  acquire_after_release: ["hijack_id", "lease_expires_at"],
  release_third: ["hijack_id"],
  to_open: ["created_at", "connected", "lifecycle_state"],
};

/**
 * The probes whose refusal this port words differently.
 *
 * A malformed path parameter is a 422 in both, and the reference's body is its
 * framework's per-field validation list. Reproducing that envelope is a unit
 * of its own and no scenario reads it, so the status is held exactly and the
 * body is this port's own one-string `detail` — the same divergence the query
 * validators already carry.
 */
const STATUS_ONLY: ReadonlySet<string> = new Set(["malformed_worker_id", "malformed_hijack_id"]);

/** Replace every declared path, exactly as the generator and harness do. */
function mask(value: unknown, paths: readonly string[]): unknown {
  if (paths.includes("*")) {
    return CORPUS.volatile;
  }
  const copy = structuredClone(value);
  for (const path of paths) {
    if (typeof copy === "object" && copy !== null && path in copy) {
      (copy as Record<string, unknown>)[path] = CORPUS.volatile;
    }
  }
  return copy;
}

/** The base a `Request` is built against. Never reaches the wire. */
const BASE = "http://127.0.0.1:0";

/** A server with its configured session started and attached to the hub. */
async function running(now = 1_700_000_000) {
  const bootstrapped = bootstrapServer({ authMode: "dev_token", now: () => now });
  await bootstrapped.runtimes.startAutoStart();
  return bootstrapped;
}

/** What a probe's `auth` means on the wire. */
function authHeader(auth: string, token: string): Record<string, string> {
  return auth === "none" ? {} : { Authorization: `Bearer ${token}` };
}

/** One request, in the shape a probe describes it. */
function requestFor(probe: Probe, path: string, token: string): Request {
  const headers = authHeader(probe.auth, token);
  if (probe.json === undefined) {
    return new Request(`${BASE}${path}`, { method: probe.method, headers });
  }
  return new Request(`${BASE}${path}`, {
    method: probe.method,
    headers: { ...headers, "content-type": "application/json" },
    body: JSON.stringify(probe.json),
  });
}

/** A path with its one `${id.body.field}` reference substituted. */
function resolve(path: string, seen: Map<string, Record<string, unknown>>): string {
  return path.replace(/\$\{(\w+)\.body\.(\w+)\}/g, (_whole, id: string, field: string) =>
    String((seen.get(id) as Record<string, unknown>)[field]),
  );
}

/** A token for a principal the stub IdP would not mint. */
function tokenFor(
  auth: { jwt_public_key_pem: string | null; jwt_issuer: string; jwt_audience: string },
  roles: string[],
) {
  return encodeJwt(
    {
      sub: "someone",
      iss: auth.jwt_issuer,
      aud: auth.jwt_audience,
      iat: 1_700_000_000,
      exp: 1_700_000_000 + 3600,
      roles,
    },
    auth.jwt_public_key_pem as string,
  );
}

describe("the reference's own answers, in the order it gave them", () => {
  it("covers every probe the reference recorded", async () => {
    const { app, token } = await running();
    const seen = new Map<string, Record<string, unknown>>();
    expect(CORPUS.probes.length).toBe(29);

    for (const probe of CORPUS.probes) {
      const path = resolve(probe.path, seen);
      const response = await app.handle(requestFor(probe, path, token));
      const body: unknown = await response.json();
      seen.set(probe.id, body as Record<string, unknown>);

      expect(`${probe.id}: ${response.status}`).toBe(`${probe.id}: ${probe.status}`);
      if (STATUS_ONLY.has(probe.id)) {
        continue;
      }
      expect({ id: probe.id, body: mask(body, VOLATILE_PATHS[probe.id] ?? []) }).toEqual({
        id: probe.id,
        body: probe.body,
      });
      if (probe.body_keys !== undefined) {
        expect({ id: probe.id, keys: Object.keys(body as object).sort() }).toEqual({
          id: probe.id,
          keys: probe.body_keys,
        });
      }
    }
  });
});

describe("reading a path", () => {
  it("takes nothing that is not a worker's hijack path", () => {
    expect(parseHijackPath("/api/sessions")).toBeUndefined();
    expect(parseHijackPath("/worker/w1/gui/attach")).toBeUndefined();
    expect(parseHijackPath("/worker/w1")).toBeUndefined();
    expect(parseHijackPath("/worker/w1/hijack/a/b/c")).toBeUndefined();
  });

  it("separates the acquire, which names no lease, from the rest, which do", () => {
    expect(parseHijackPath("/worker/w1/hijack/acquire")).toEqual({ workerId: "w1", verb: "acquire" });
    expect(parseHijackPath("/worker/w1/hijack/abc/release")).toEqual({
      workerId: "w1",
      hijackId: "abc",
      verb: "release",
    });
  });
});

describe("what the reference cannot be made to answer from outside", () => {
  it("refuses a verb nobody defined as absent rather than forbidden", async () => {
    const { app, token } = await running();
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/acquire-please`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      }),
    );
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ detail: "Not Found" });
  });

  it("tells a caller to fix its verb rather than sending it looking elsewhere", async () => {
    const { app, token } = await running();
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/acquire`, {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      }),
    );
    expect(response.status).toBe(405);
    expect(response.headers.get("allow")).toBe("POST");
  });

  it("refuses a viewer the lease, and by privilege rather than by absence", async () => {
    // A viewer holds `session.read` and not `session.control.hijack`, so the
    // read route answers and the driving one does not.
    const { app, auth } = await running();
    const viewer = tokenFor(auth, ["viewer"]);
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/acquire`, {
        method: "POST",
        headers: { Authorization: `Bearer ${viewer}` },
        body: "{}",
      }),
    );
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "insufficient privileges" });
  });

  it("refuses a viewer a read of a session it may not see", async () => {
    const { app, auth, registry } = await running();
    // The default session is public; made private, a viewer who neither owns
    // nor administers it may not read through the lease either.
    const definition = registry.definition("provide-shell");
    Object.assign(definition as object, { visibility: "private" });
    const viewer = tokenFor(auth, ["viewer"]);
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/00000000-0000-0000-0000-000000000000/snapshot`, {
        headers: { Authorization: `Bearer ${viewer}` },
      }),
    );
    expect(response.status).toBe(403);
  });

  it("takes an acquire that names neither an owner nor a lease length", async () => {
    // The reference's model defaults: `operator`, ninety seconds.
    const { app, token, registry, hub } = await running();
    registry.setInputMode("provide-shell", "hijack");
    await hub.setInputMode("provide-shell", "hijack");
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/acquire`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      }),
    );
    expect(response.status).toBe(200);
    expect(((await response.json()) as { owner: string }).owner).toBe("operator");
  });

  it("refuses an acquire for a session whose worker has gone", async () => {
    const { app, token, registry, hub } = await running();
    registry.setInputMode("provide-shell", "hijack");
    await hub.setInputMode("provide-shell", "hijack");
    // The definition stays — the session is still configured — but nothing is
    // attached, which is the state a worker's socket dropping leaves behind.
    const state = hub.registry.get("provide-shell");
    (state as { workerWs: unknown }).workerWs = undefined;
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/acquire`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: JSON.stringify({ owner: "x", lease_s: 60 }),
      }),
    );
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "No worker connected for this session." });
  });
});

describe("the session routes the lease depends on", () => {
  it("answers a screen nobody has produced with a 200 and nothing, not a 404", async () => {
    // The session exists and has nothing to show yet, which is a different
    // thing from a session that does not exist.
    const bootstrapped = bootstrapServer({ authMode: "dev_token", now: () => 1_700_000_000 });
    const response = await bootstrapped.app.handle(
      new Request(`${BASE}/api/sessions/provide-shell/snapshot`, {
        headers: { Authorization: `Bearer ${bootstrapped.token}` },
      }),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toBeNull();
  });

  it("refuses a reader who may not see the session, without saying it is absent", async () => {
    const { app, auth, registry } = await running();
    Object.assign(registry.definition("provide-shell") as object, { visibility: "private" });
    const response = await app.handle(
      new Request(`${BASE}/api/sessions/provide-shell/snapshot`, {
        headers: { Authorization: `Bearer ${tokenFor(auth, ["viewer"])}` },
      }),
    );
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "insufficient privileges" });
  });

  it("refuses a mode change to someone who may read the session but not change it", async () => {
    const { app, auth } = await running();
    const response = await app.handle(
      new Request(`${BASE}/api/sessions/provide-shell/mode`, {
        method: "POST",
        headers: { Authorization: `Bearer ${tokenFor(auth, ["viewer"])}` },
        body: JSON.stringify({ input_mode: "hijack" }),
      }),
    );
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "insufficient privileges" });
  });

  it("releases whatever is held when the session is opened to everyone", async () => {
    // In open mode the lease stops gating input, so leaving one in place
    // would put its holder in the position of believing they alone are
    // driving a terminal everyone can now type into.
    const { app, token, hijackId, hub } = await leased();
    const opened = await app.handle(
      new Request(`${BASE}/api/sessions/provide-shell/mode`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: JSON.stringify({ input_mode: "open" }),
      }),
    );

    expect(opened.status).toBe(200);
    expect(hub.registry.get("provide-shell")?.hijackSession).toBeUndefined();
    const stale = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/${hijackId}/heartbeat`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      }),
    );
    expect(stale.status).toBe(404);
  });

  it("leaves a mode change for a session nobody configured with nothing to change", async () => {
    // `setInputMode` tolerates a session that has gone the way `setState`
    // does, and for the same reason: it is a race, not a fault.
    const { registry } = await running();
    registry.setInputMode("no-such-session", "hijack");
    expect(registry.definition("no-such-session")).toBeUndefined();
  });
});

/** A server holding a lease, and everything a test needs to act through it. */
async function leased(owner = "conformance") {
  const bootstrapped = await running();
  bootstrapped.registry.setInputMode("provide-shell", "hijack");
  await bootstrapped.hub.setInputMode("provide-shell", "hijack");
  const response = await bootstrapped.app.handle(
    new Request(`${BASE}/worker/provide-shell/hijack/acquire`, {
      method: "POST",
      headers: { Authorization: `Bearer ${bootstrapped.token}` },
      body: JSON.stringify({ owner, lease_s: 60 }),
    }),
  );
  const { hijack_id } = (await response.json()) as { hijack_id: string };
  const through = (verb: string, init: RequestInit = {}) =>
    bootstrapped.app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/${hijack_id}/${verb}`, {
        method: verb === "snapshot" ? "GET" : "POST",
        ...init,
        headers: { Authorization: `Bearer ${bootstrapped.token}`, ...(init.headers ?? {}) },
      }),
    );
  return { ...bootstrapped, hijackId: hijack_id, through };
}

describe("typing through a lease", () => {
  it("refuses keystrokes nobody sent", async () => {
    const { through } = await leased();
    const response = await through("send", { body: "{}" });
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "keys must not be empty." });
  });

  it("refuses a paste longer than the hub will carry", async () => {
    const { through, hub } = await leased();
    const keys = "x".repeat(hub.maxInputChars + 1);
    const response = await through("send", { body: JSON.stringify({ keys }) });
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: `keys too long: ${keys.length} > ${hub.maxInputChars}` });
  });

  it("waits for the prompt a caller named, and says which it saw when it never came", async () => {
    const { through } = await leased();
    const response = await through("send", {
      body: JSON.stringify({ keys: "x", expect_prompt_id: "never", timeout_ms: 50, poll_interval_ms: 20 }),
    });
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({
      error: "prompt_guard_not_satisfied",
      current_prompt_id: "shell_prompt",
    });
  });

  it("reports a guard it will not compile as a refusal rather than as a timeout", async () => {
    // A caller needs to know their pattern was refused, not that the screen
    // never matched it.
    const { through } = await leased();
    const response = await through("send", {
      body: JSON.stringify({ keys: "x", expect_regex: "(a+)+$", timeout_ms: 50, poll_interval_ms: 20 }),
    });
    expect(response.status).toBe(409);
    expect(String(((await response.json()) as { error: string }).error)).toContain("pattern");
  });

  it("says so when the worker went away between the guard and the keystrokes", async () => {
    const { through, hub } = await leased();
    (hub.registry.get("provide-shell") as { workerWs: unknown }).workerWs = undefined;
    const response = await through("send", { body: JSON.stringify({ keys: "x" }) });
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "No worker connected for this session." });
  });

  it("refuses to type on a lease that lapsed inside the guard's own wait", async () => {
    // The re-check exists for a race no test can schedule: the lease is live
    // when the guard starts and gone when it returns. Provoked directly, so
    // what is under test is the route's answer rather than the timing.
    const { through, hub } = await leased();
    vi.spyOn(hub.lease, "checkValid").mockResolvedValue(false);
    const response = await through("send", { body: JSON.stringify({ keys: "x" }) });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Invalid or expired hijack session." });
  });

  it("reports no prompt rather than inventing one when the screen has none", async () => {
    const { through, hub } = await leased();
    await hub.updateLastSnapshot("provide-shell", { type: "snapshot", screen: "", ts: Date.now() });
    const response = await through("send", { body: JSON.stringify({ keys: "x" }) });
    expect(((await response.json()) as { matched_prompt_id: unknown }).matched_prompt_id).toBeNull();
  });
});

describe("stepping through a lease", () => {
  it("says so when the worker has gone", async () => {
    const { through, hub } = await leased();
    (hub.registry.get("provide-shell") as { workerWs: unknown }).workerWs = undefined;
    const response = await through("step");
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "No worker connected for this session." });
  });

  it("refuses to step a lease that lapsed under it", async () => {
    const { through, hub } = await leased();
    vi.spyOn(hub.lease, "checkValid").mockResolvedValue(false);
    expect((await through("step")).status).toBe(404);
  });
});

describe("keeping a lease alive", () => {
  it("takes a heartbeat that names no length, and uses the library's own", async () => {
    const { through } = await leased();
    const response = await through("heartbeat", { body: "{}" });
    expect(response.status).toBe(200);
    expect(((await response.json()) as { ok: boolean }).ok).toBe(true);
  });

  it("takes a body that is not an object at all as one that said nothing", async () => {
    const { through } = await leased();
    expect((await through("heartbeat", { body: "5" })).status).toBe(200);
  });

  it("refuses to extend a lease that lapsed under it", async () => {
    const { through, hub } = await leased();
    vi.spyOn(hub.lease, "extendLease").mockResolvedValue(undefined);
    const response = await through("heartbeat", { body: "{}" });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Invalid or expired hijack session." });
  });
});

describe("giving a lease back", () => {
  it("refuses anyone but whoever took it", async () => {
    const { app, auth, hijackId } = await leased();
    // Another administrator, and still not the holder: the lease routes check
    // the acquiring principal, not merely the capability to reach the route.
    const other = tokenFor(auth, ["admin"]);
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/${hijackId}/release`, {
        method: "POST",
        headers: { Authorization: `Bearer ${other}` },
      }),
    );
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ error: "Not the lease owner." });
  });

  it("lets anyone holding the id drop a lease that recorded no acquirer", async () => {
    // The older capability model, kept for a lease taken by a path that
    // records no principal: possession of the unguessable id is the right.
    const { app, auth, hijackId, hub } = await leased();
    const session = hub.registry.get("provide-shell")?.hijackSession as { acquiredBy?: string | undefined };
    session.acquiredBy = undefined;
    const response = await app.handle(
      new Request(`${BASE}/worker/provide-shell/hijack/${hijackId}/release`, {
        method: "POST",
        headers: { Authorization: `Bearer ${tokenFor(auth, ["admin"])}` },
      }),
    );
    expect(response.status).toBe(200);
  });

  it("leaves the worker paused when a browser is still driving it", async () => {
    const { through, hub } = await leased();
    // A dashboard hold beside the REST lease. Only one of the two can be
    // *taken* at a time, but a REST lease released while a browser still
    // holds the session must not unpause the worker under it.
    const state = hub.registry.get("provide-shell") as {
      hijackOwner: unknown;
      hijackOwnerExpiresAt: number;
    };
    state.hijackOwner = {};
    state.hijackOwnerExpiresAt = hub.monotonic() + 30;
    const sends = vi.spyOn(hub, "sendWorker");

    const response = await through("release");

    expect(response.status).toBe(200);
    expect(sends.mock.calls.filter(([, message]) => message.action === "resume")).toEqual([]);
    expect(hub.registry.get("provide-shell")?.hijackSession).toBeUndefined();
  });

  it("refuses a release the lease manager says it cannot make", async () => {
    const { through, hub } = await leased();
    vi.spyOn(hub.lease, "releaseRest").mockResolvedValue({ ok: false, shouldResume: false });
    expect((await through("release")).status).toBe(404);
  });

  it("leaves the worker paused when somebody took it in the same instant", async () => {
    const { through, hub } = await leased();
    vi.spyOn(hub.lease, "stillHijacked").mockResolvedValue(true);
    const sends = vi.spyOn(hub, "sendWorker");
    expect((await through("release")).status).toBe(200);
    expect(sends.mock.calls.filter(([, message]) => message.action === "resume")).toEqual([]);
  });
});

/**
 * The rate limiter, on a clock the test moves.
 *
 * Nothing here sleeps. A window that rolled over because a test waited out a
 * real second is a test that fails on a loaded machine and passes on a quiet
 * one; the hub's monotonic clock is injected, the limiter's buckets refill
 * against it, and the window is therefore driven rather than waited for.
 *
 * These go through `handleHijackRequest` rather than the application, because
 * the clock has to reach the hub and `bootstrapServer` builds its own.
 */
describe("charging a caller's address for what it asks", () => {
  /** A hub whose clock only moves when the test moves it, worker attached. */
  function metered() {
    const clock = { mono: 1000 };
    const hub = new SessionHub({
      now: () => clock.mono,
      wallNow: () => 1_700_000_000,
      // A poll that waits spends the hub's own clock rather than the wall's,
      // so a snapshot's wait finishes instead of hanging on a frozen one.
      sleep: async (seconds) => {
        clock.mono += seconds;
      },
    });
    hub.registerWorker("provide-shell", { sendText: async () => {} }, "hijack");
    const { registry } = bootstrapServer({ authMode: "jwt", now: () => 1_700_000_000 });
    const principal = { subject_id: "someone", roles: new Set(["admin"]), scopes: new Set(["*"]) };
    /** An asker fixed to one address, so an absent one stays absent. */
    const from =
      (clientAddress: string | undefined) =>
      async (path: string): Promise<Response> => {
        const reads = path.endsWith("snapshot");
        const answer = await handleHijackRequest(
          new Request(`${BASE}/worker/provide-shell/hijack/${path}`, {
            method: reads ? "GET" : "POST",
            ...(reads ? {} : { body: "{}" }),
          }),
          { hub, registry, principal, authenticated: true, clientAddress },
        );
        return answer as Response;
      };
    return { clock, hub, from, ask: from("10.0.0.1") };
  }

  /** The statuses one path answered with, asked `count` times over. */
  async function statuses(ask: (path: string) => Promise<Response>, path: string, count: number): Promise<number[]> {
    const seen: number[] = [];
    for (let index = 0; index < count; index += 1) {
      seen.push((await ask(path)).status);
    }
    return seen;
  }

  it("lets five acquires through and refuses the sixth", async () => {
    const { ask } = metered();
    // Five is the whole budget and the sixth is over it: the burst defaults to
    // one second of capacity, so a caller that spends it in one instant has
    // nothing left until the window moves. The four refusals in between are
    // the lease's own — the first acquire took it — and each one still cost a
    // token, which is what makes the sixth a 429 rather than a fifth 409.
    expect(await statuses(ask, "acquire", 6)).toEqual([200, 409, 409, 409, 409, 429]);
  });

  it("says only that the caller was rate limited, in the lease routes' envelope", async () => {
    const { ask } = metered();
    await statuses(ask, "acquire", 5);
    const refused = await ask("acquire");
    expect(refused.status).toBe(429);
    expect(await refused.json()).toEqual({ error: "rate_limited" });
    // No `Retry-After`. The reference sends none, and inventing one here would
    // name a time this server never agreed to.
    expect(refused.headers.get("retry-after")).toBeNull();
  });

  it("restores the budget as the window moves, and not before", async () => {
    const { ask, clock } = metered();
    await statuses(ask, "acquire", 5);
    expect((await ask("acquire")).status).toBe(429);
    // A fifth of a second is one token at five a second, and not two.
    clock.mono += 0.2;
    expect((await ask("acquire")).status).not.toBe(429);
    expect((await ask("acquire")).status).toBe(429);
    clock.mono += 1;
    expect(await statuses(ask, "acquire", 5)).not.toContain(429);
    expect((await ask("acquire")).status).toBe(429);
  });

  it("refuses a second address that the first one's flood spent the budget on", async () => {
    const { ask, from } = metered();
    // Not isolation, and the reference is the same: there is a shared bucket
    // behind the per-address ones and it is the same size, so it is always the
    // binding limit. One noisy address does deny another one service.
    await statuses(ask, "acquire", 5);
    expect((await from("10.0.0.2")("acquire")).status).toBe(429);
  });

  it("hands the refilled budget to whoever asks for it, flooder or not", async () => {
    const { ask, from, clock } = metered();
    await statuses(ask, "acquire", 12);
    clock.mono += 0.2;
    // The flooder's own bucket ran out in the same instant the shared one did,
    // so the one refilled token is not reserved for it.
    expect((await from("10.0.0.2")("acquire")).status).not.toBe(429);
    expect((await ask("acquire")).status).toBe(429);
  });

  it("charges sends and steps against one budget, and acquires against another", async () => {
    const { ask } = metered();
    // Twenty a second is the send policy, and a step spends from it — two
    // names for one budget, which is what the reference does.
    expect(await statuses(ask, "deadbeef/send", 20)).not.toContain(429);
    expect((await ask("deadbeef/send")).status).toBe(429);
    expect((await ask("deadbeef/step")).status).toBe(429);
    // None of which touched the acquire budget.
    expect((await ask("acquire")).status).toBe(200);
  });

  it("charges a step against the send budget rather than the acquire one", async () => {
    const { ask } = metered();
    expect(await statuses(ask, "deadbeef/step", 20)).not.toContain(429);
    expect((await ask("deadbeef/step")).status).toBe(429);
    expect((await ask("acquire")).status).toBe(200);
  });

  it("refuses over budget before it looks the lease up", async () => {
    const { ask } = metered();
    // A lease nobody holds is a 404 on its merits. Over the budget the same
    // request is a 429, because the limiter runs first — and which of the two
    // a caller gets is how the order becomes observable.
    expect((await ask("deadbeef/send")).status).toBe(404);
    await statuses(ask, "deadbeef/send", 19);
    expect((await ask("deadbeef/send")).status).toBe(429);
  });

  it("charges nothing for the refusals the gate makes before it", async () => {
    const { ask, hub } = metered();
    const anonymous = await handleHijackRequest(
      new Request(`${BASE}/worker/provide-shell/hijack/acquire`, { method: "POST" }),
      {
        hub,
        registry: bootstrapServer({ authMode: "jwt" }).registry,
        principal: { subject_id: "someone", roles: new Set(["admin"]), scopes: new Set(["*"]) },
        authenticated: false,
        clientAddress: "10.0.0.1",
      },
    );
    // 401 and not 429, and no token spent: an unauthenticated flood would
    // learn the budget only if the limiter ran in front of the gate, and it
    // does not — the whole budget is still there afterwards.
    expect(anonymous?.status).toBe(401);
    expect(hub.limiter.restAcquireClientCount).toBe(0);
    expect(await statuses(ask, "acquire", 5)).not.toContain(429);
  });

  it("leaves a lease the caller already holds alone when it refuses their send", async () => {
    const { ask, hub } = metered();
    const taken = (await (await ask("acquire")).json()) as { hijack_id: string };
    await statuses(ask, `${taken.hijack_id}/send`, 20);
    expect((await ask(`${taken.hijack_id}/send`)).status).toBe(429);
    // Still held, still theirs, and still workable. A refusal refuses an
    // action; it is not a reason to drop what the caller was granted — and
    // none of heartbeat, snapshot or release is charged, so a held lease
    // cannot be rate limited into expiring.
    expect(hub.registry.get("provide-shell")?.hijackSession?.hijackId).toBe(taken.hijack_id);
    expect((await ask(`${taken.hijack_id}/heartbeat`)).status).toBe(200);
    expect((await ask(`${taken.hijack_id}/snapshot`)).status).toBe(200);
    expect((await ask(`${taken.hijack_id}/release`)).status).toBe(200);
  });

  it("keys a caller whose address it was never told as one shared `unknown`", async () => {
    const { from, hub } = metered();
    // One bucket and not one per unnamed caller: a caller whose address the
    // runtime could not report would otherwise be the one with no limit.
    await from(undefined)("acquire");
    expect(hub.limiter.hasRestAcquireClient("unknown")).toBe(true);
    await from("")("acquire");
    expect(hub.limiter.restAcquireClientCount).toBe(1);
  });
});
