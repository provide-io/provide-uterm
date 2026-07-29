//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The whole HTTP surface, held to `serverhttp_golden`.
 *
 * The corpus is not a set of decisions a function made — it is what the
 * reference *server* answered, recorded off a real socket with the default
 * configuration in `dev_token` mode. That is the thing this port has to be
 * indistinguishable from: the live matrix compares a cell field-for-field
 * against the Python one, so a field nobody wrote an expectation for still
 * has to agree.
 *
 * Values that legitimately differ between two runs — a clock, a version, a
 * counter — are masked with the same marker and along the same paths the
 * scenarios declare volatile. Everything else is compared exactly.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { BAD_TOKEN } from "../conformance/transport.ts";
import { encodeJwt } from "../serverauth/index.ts";
import { createServerApp, parseListQuery, SERVED_ROUTES, UNAUTHENTICATED_DETAIL, unservedCapability } from "./app.ts";
import { bootstrapServer, SERVER_BOOTSTRAP_HOST, SERVER_VERSION, ServerBootstrapError } from "./bootstrap.ts";
import { SessionRegistry } from "./session-registry.ts";
import { sessionDefinitionFrom } from "./session-status.ts";

interface Probe {
  id: string;
  method: string;
  path: string;
  auth: string;
  status: number;
  headers: Record<string, string>;
  body: unknown;
}

const CORPUS = JSON.parse(
  readFileSync(join(import.meta.dirname, "..", "..", "testdata", "serverhttp_golden.json"), "utf8"),
) as { volatile: string; probes: Probe[] };

/** The paths whose value is a clock, a counter, or how far startup got. */
const VOLATILE_PATHS: Readonly<Record<string, readonly string[]>> = {
  health_anonymous: ["version", "uptime_s", "active_sessions"],
  health_token: ["version", "uptime_s", "active_sessions"],
  health_forged: ["version", "uptime_s", "active_sessions"],
  sessions_token: ["*.created_at", "*.lifecycle_state", "*.connected"],
  session_token: ["created_at", "lifecycle_state", "connected"],
};

/** Replace every declared path, exactly as the generator and harness do. */
function mask(value: unknown, paths: readonly string[]): unknown {
  const copy = structuredClone(value);
  for (const path of paths) {
    maskOne(copy, path.split("."));
  }
  return copy;
}

function maskOne(node: unknown, segments: readonly string[]): void {
  const [head, ...rest] = segments as [string, ...string[]];
  if (typeof node !== "object" || node === null) {
    return;
  }
  const container = node as Record<string, unknown>;
  const keys = head === "*" ? Object.keys(container) : head in container ? [head] : [];
  for (const key of keys) {
    if (rest.length > 0) {
      maskOne(container[key], rest);
    } else {
      container[key] = CORPUS.volatile;
    }
  }
}

/** What a probe's `auth` means on the wire. */
function authHeader(auth: string, token: string): Record<string, string> {
  if (auth === "none") {
    return {};
  }
  if (auth === "bad") {
    return { Authorization: `Bearer ${BAD_TOKEN}` };
  }
  if (auth === "bare") {
    return { Authorization: "Bearer" };
  }
  if (auth === "basic") {
    return { Authorization: `Basic ${token}` };
  }
  return { Authorization: `Bearer ${token}` };
}

/** A server on the default configuration, with a clock nobody has to wait for. */
function server(now = 1_700_000_000) {
  return bootstrapServer({ authMode: "dev_token", now: () => now });
}

/**
 * A token for a principal the stub IdP would not mint.
 *
 * The stub mints an administrator, which passes every authorization check
 * there is — so nothing about the checks themselves would be visible without
 * a token that holds less.
 */
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

/** The base a `Request` is built against. Never reaches the wire. */
const BASE = "http://127.0.0.1:0";

describe("the reference's own answers, probe by probe", () => {
  it("covers every probe the reference recorded", () => {
    expect(CORPUS.probes.length).toBe(17);
  });

  for (const probe of CORPUS.probes) {
    it(`${probe.id}: ${probe.method} ${probe.path} as ${probe.auth}`, async () => {
      const { app, token } = server();
      const response = await app.handle(
        new Request(`${BASE}${probe.path}`, {
          method: probe.method,
          headers: authHeader(probe.auth, token),
          ...(probe.method === "GET" ? {} : { body: "{}" }),
        }),
      );
      expect(response.status).toBe(probe.status);
      expect(mask(await response.json(), VOLATILE_PATHS[probe.id] ?? [])).toEqual(probe.body);
      expect(response.headers.get("content-type")).toBe(probe.headers["content-type"]);
    });
  }
});

describe("where this port answers differently, and why", () => {
  it("advertises only the verbs it binds when it refuses a method", async () => {
    // The reference binds the whole session family, so its Allow header on
    // this path names three verbs. This port binds the read half, and a
    // router that advertised operations it cannot serve would send a client
    // to a verb that 404s. Stated as an assertion so the gap cannot close by
    // accident and go unnoticed.
    const reference = CORPUS.probes.find((probe) => probe.id === "wrong_method_session") as Probe;
    expect(reference.headers.allow).toBe("DELETE, GET, PATCH");

    const { app, token } = server();
    const response = await app.handle(
      new Request(`${BASE}/api/sessions/provide-shell`, {
        method: "PUT",
        headers: authHeader("token", token),
        body: "{}",
      }),
    );
    expect(response.status).toBe(reference.status);
    expect(response.headers.get("allow")).toBe("GET");
    expect(SERVED_ROUTES.map((route) => route.capability)).toEqual(["sessions.list", "sessions.get"]);
  });

  it("matches the reference's Allow header where it binds the same verbs", async () => {
    const reference = CORPUS.probes.find((probe) => probe.id === "wrong_method_health") as Probe;
    const { app } = server();
    const response = await app.handle(new Request(`${BASE}/api/health`, { method: "POST", body: "{}" }));
    expect(response.headers.get("allow")).toBe(reference.headers.allow);
  });
});

describe("the orderings the refusals depend on", () => {
  it("decides authentication before existence", async () => {
    // Byte-identical to the refusal for a session that does exist: otherwise
    // an anonymous caller could enumerate session ids by status code alone.
    const { app } = server();
    const unknown = await app.handle(new Request(`${BASE}/api/sessions/no-such-session`));
    const known = await app.handle(new Request(`${BASE}/api/sessions/provide-shell`));
    expect(unknown.status).toBe(401);
    expect(known.status).toBe(401);
    expect(await unknown.json()).toEqual(await known.json());
  });

  it("decides routing before authentication", async () => {
    // A path in no route table is a 404 whether or not a token was presented,
    // which is what the reference's framework does.
    const { app, token } = server();
    const anonymous = await app.handle(new Request(`${BASE}/api/not-a-thing`));
    const authenticated = await app.handle(
      new Request(`${BASE}/api/not-a-thing`, { headers: authHeader("token", token) }),
    );
    expect(anonymous.status).toBe(404);
    expect(await anonymous.json()).toEqual(await authenticated.json());
  });

  it("refuses a forged credential in the same words as no credential", async () => {
    const { app } = server();
    const none = await app.handle(new Request(`${BASE}/api/sessions`));
    const forged = await app.handle(new Request(`${BASE}/api/sessions`, { headers: authHeader("bad", "") }));
    expect(await forged.text()).toBe(await none.text());
    expect(forged.status).toBe(none.status);
  });

  it("names the refusal exactly once, so no port invents its own wording", async () => {
    const { app } = server();
    expect(await (await app.handle(new Request(`${BASE}/api/sessions`))).json()).toEqual({
      detail: UNAUTHENTICATED_DETAIL,
    });
  });
});

describe("what a probe sees before the server is ready", () => {
  it("is a 503 that says it is starting", async () => {
    const { app } = server();
    app.ready = false;
    const health = await app.handle(new Request(`${BASE}/api/health`));
    expect(health.status).toBe(503);
    expect(await health.json()).toEqual({ status: "starting", ok: false, ready: false, service: "uterm-server" });
  });

  it("is a readiness probe that says not ready, while liveness still says ok", async () => {
    const { app } = server();
    app.ready = false;
    expect(app.ready).toBe(false);
    const ready = await app.handle(new Request(`${BASE}/readyz`));
    expect(ready.status).toBe(503);
    expect(await ready.json()).toEqual({ status: "not_ready" });
    const live = await app.handle(new Request(`${BASE}/healthz`));
    expect(live.status).toBe(200);
  });

  it("refuses a wrong method on a probe rather than pretending it is not there", async () => {
    const { app } = server();
    const response = await app.handle(new Request(`${BASE}/healthz`, { method: "DELETE" }));
    expect(response.status).toBe(405);
    expect(response.headers.get("allow")).toBe("GET");
  });
});

describe("listing sessions", () => {
  // One bootstrap, so every app below verifies the same token: a second one
  // would mint its own secret and refuse the first one's credential.
  const shared = server();

  /** A server carrying more than the one configured session. */
  function withSessions(entries: Record<string, unknown>[]) {
    const auth = shared.auth;
    const registry = new SessionRegistry(
      entries.map((entry, index) => sessionDefinitionFrom(entry, `2026-01-0${index + 1}T00:00:00.000Z`)),
      false,
    );
    return createServerApp({
      registry,
      auth,
      version: "0.0.0",
      controlPlaneBackend: "memory",
      startupTime: 1,
      now: () => 1_700_000_001,
    });
  }

  const three = [
    { session_id: "alpha", tags: ["one"], connector_type: "shell" },
    { session_id: "beta", tags: ["two"], connector_type: "telnet", visibility: "public" },
    { session_id: "gamma", tags: ["one", "two"], connector_type: "shell" },
  ];

  /** The ids a query answers with, as the authenticated caller. */
  async function ids(query: string): Promise<string[]> {
    const token = shared.token;
    const app = withSessions(three);
    const response = await app.handle(
      new Request(`${BASE}/api/sessions${query}`, { headers: authHeader("token", token) }),
    );
    return ((await response.json()) as { session_id: string }[]).map((one) => one.session_id);
  }

  it("is newest first by default", async () => {
    expect(await ids("")).toEqual(["gamma", "beta", "alpha"]);
  });

  it("is oldest first when asked", async () => {
    expect(await ids("?order=asc")).toEqual(["alpha", "beta", "gamma"]);
  });

  it("sorts by a named field", async () => {
    expect(await ids("?sort=session_id&order=asc")).toEqual(["alpha", "beta", "gamma"]);
  });

  it("falls back to creation order for a field nobody can sort by", async () => {
    expect(await ids("?sort=nonsense&order=asc")).toEqual(["alpha", "beta", "gamma"]);
  });

  it("sorts by nothing outside the three fields it knows", async () => {
    // `connector_type` is not sortable, so the request falls back to creation
    // order rather than failing — which is what the reference does with it.
    expect(await ids("?sort=connector_type&order=asc")).toEqual(["alpha", "beta", "gamma"]);
  });

  it("keeps the order it was given when two sort keys are equal", async () => {
    // Stability is what keeps two servers with identical data answering in
    // the same order, which a matrix comparing bodies depends on.
    const registry = new SessionRegistry(
      ["one", "two", "three"].map((id) => sessionDefinitionFrom({ session_id: id }, "2026-01-01T00:00:00.000Z")),
      false,
    );
    const app = createServerApp({
      registry,
      auth: shared.auth,
      version: "0.0.0",
      controlPlaneBackend: "memory",
      startupTime: 1,
      now: () => 1_700_000_001,
    });
    const response = await app.handle(
      new Request(`${BASE}/api/sessions`, { headers: authHeader("token", shared.token) }),
    );
    expect(((await response.json()) as { session_id: string }[]).map((one) => one.session_id)).toEqual([
      "one",
      "two",
      "three",
    ]);
  });

  it("narrows by tag, matching any of them", async () => {
    expect(await ids("?tag=two&order=asc")).toEqual(["beta", "gamma"]);
  });

  it("narrows by connector type", async () => {
    expect(await ids("?connector_type=telnet")).toEqual(["beta"]);
  });

  it("narrows by visibility", async () => {
    expect(await ids("?visibility=public&order=asc")).toEqual(["alpha", "beta", "gamma"]);
  });

  it("narrows by lifecycle state", async () => {
    expect(await ids("?state=running")).toEqual([]);
  });

  it("narrows by free text over the id, the name and the tags", async () => {
    expect(await ids("?q=BET")).toEqual(["beta"]);
    expect(await ids("?q=two&order=asc")).toEqual(["beta", "gamma"]);
  });

  it("pages", async () => {
    expect(await ids("?order=asc&limit=1")).toEqual(["alpha"]);
    expect(await ids("?order=asc&limit=1&offset=2")).toEqual(["gamma"]);
  });

  it("refuses a page size outside its bounds rather than clamping it", async () => {
    const token = shared.token;
    const app = withSessions(three);
    for (const query of ["?limit=0", "?limit=201", "?limit=x", "?offset=-1", `?q=${"x".repeat(201)}`]) {
      const response = await app.handle(
        new Request(`${BASE}/api/sessions${query}`, { headers: authHeader("token", token) }),
      );
      expect(response.status).toBe(422);
    }
  });

  it("hides a session the caller may not read", async () => {
    // The list is filtered by what this caller can read before it is paged,
    // so a page is never spent on sessions that are then hidden.
    const app = withSessions([{ session_id: "secret", visibility: "private" }]);
    const response = await app.handle(
      new Request(`${BASE}/api/sessions`, { headers: authHeader("token", tokenFor(shared.auth, ["viewer"])) }),
    );
    expect(await response.json()).toEqual([]);
  });
});

describe("fetching one session", () => {
  it("refuses one the caller may not read, without saying it is absent", async () => {
    // 403 rather than 404: the caller is known, the thing exists, and the
    // answer is about privilege. The reference draws the same line.
    const { auth } = server();
    const token = tokenFor(auth, ["viewer"]);
    const registry = new SessionRegistry(
      [sessionDefinitionFrom({ session_id: "secret", visibility: "private" }, "2026-01-01T00:00:00.000Z")],
      false,
    );
    const app = createServerApp({
      registry,
      auth,
      version: "0.0.0",
      controlPlaneBackend: "memory",
      startupTime: 0,
      now: () => 1_700_000_001,
    });
    const response = await app.handle(
      new Request(`${BASE}/api/sessions/secret`, { headers: authHeader("token", token) }),
    );
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "insufficient privileges" });
  });

  it("refuses a session id the route grammar has no reading for", async () => {
    const { app, token } = server();
    const response = await app.handle(
      new Request(`${BASE}/api/sessions/not.a.session.id`, { headers: authHeader("token", token) }),
    );
    expect(response.status).toBe(422);
  });

  it("reports an uptime of zero when nothing recorded a start", async () => {
    const { auth } = server();
    const app = createServerApp({
      registry: new SessionRegistry([], false),
      auth,
      version: "0.0.0",
      controlPlaneBackend: "memory",
      startupTime: 0,
    });
    const health = (await (await app.handle(new Request(`${BASE}/api/health`))).json()) as { uptime_s: number };
    expect(health.uptime_s).toBe(0);
  });
});

describe("a capability nobody bound", () => {
  it("raises rather than answering, because reaching it is a fault in the wiring", async () => {
    // Every capability in the shared inventory needs an entry or the binding
    // refuses outright. The ones this server does not serve are never
    // selected, so nothing can reach this — and if something did, a status
    // code would tell a client the wrong story about why.
    await expect(unservedCapability()).rejects.toThrow("unregistered shared API capability invoked");
  });
});

describe("reading a list query", () => {
  it("takes the reference's own defaults when nothing was asked for", () => {
    expect(parseListQuery(new URLSearchParams())).toEqual({
      tag: [],
      connector_type: undefined,
      visibility: undefined,
      state: undefined,
      q: undefined,
      sort: "created_at",
      order: "desc",
      limit: 50,
      offset: 0,
    });
  });
});

describe("bootstrapping", () => {
  it("reports the package's own version, which is the reference's field for field", () => {
    // Held against package.json rather than read from it, so nothing in the
    // request path has to reach a filesystem a Worker does not have — and so
    // a release that bumped one and not the other fails here.
    const manifest = JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "package.json"), "utf8")) as {
      version: string;
    };
    expect(SERVER_VERSION).toBe(manifest.version);
  });

  it("refuses dev_token on an address something else could reach", async () => {
    // A refusal rather than a warning: a warning in a startup log is a thing
    // nobody reads until afterwards, and by then the stub IdP is listening on
    // a routable address.
    expect(() => bootstrapServer({ authMode: "dev_token", document: { server: { host: "0.0.0.0" } } })).toThrow(
      ServerBootstrapError,
    );
  });

  it("allows dev_token on each of the loopback names", () => {
    for (const host of SERVER_BOOTSTRAP_HOST) {
      expect(() => bootstrapServer({ authMode: "dev_token", document: { server: { host } } })).not.toThrow();
    }
  });

  it("refuses the modes that were removed", () => {
    for (const mode of ["dev", "none"]) {
      expect(() => bootstrapServer({ authMode: mode })).toThrow(ServerBootstrapError);
    }
  });

  it("runs in whatever mode the configuration named when nobody overrode it", () => {
    // The default configuration names `dev_token`, so a bootstrap with no
    // mode argument is the one a deployment gets.
    expect(bootstrapServer().auth.mode).toBe("jwt");
    expect(bootstrapServer({ document: { auth: { mode: "jwt" } } }).token).toBe("");
  });

  it("mints no token in a mode that has no stub identity provider", () => {
    const { token, auth } = bootstrapServer({ authMode: "jwt" });
    expect(token).toBe("");
    expect(auth.mode).toBe("jwt");
  });

  it("collapses dev_token to jwt, so there is one authentication path", () => {
    const { auth, token } = server();
    expect(auth.mode).toBe("jwt");
    expect(token).not.toBe("");
  });

  it("takes the configured sessions, and the control-plane backend health reports", async () => {
    const { app, registry } = bootstrapServer({
      authMode: "jwt",
      document: { control_plane: { backend: "sqlite" }, sessions: [{ session_id: "one" }] },
    });
    expect(registry.definitions().map((one) => one.session_id)).toEqual(["one"]);
    const health = (await (await app.handle(new Request(`${BASE}/api/health`))).json()) as Record<string, unknown>;
    expect(health.control_plane_backend).toBe("sqlite");
    expect(health.active_sessions).toBe(1);
  });

  it("reads the clock itself when nobody hands it one", () => {
    const before = Math.trunc(Date.now() / 1000);
    const { registry } = bootstrapServer({ authMode: "jwt" });
    const created = Date.parse(registry.definitions()[0]?.created_at as string) / 1000;
    expect(created).toBeGreaterThanOrEqual(before - 1);
  });
});
