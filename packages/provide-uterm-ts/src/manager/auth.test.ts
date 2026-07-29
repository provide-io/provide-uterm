//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type AsgiScope,
  authorizeScope,
  deriveAgentToken,
  extractRequestToken,
  extractSelfReportAgentId,
  isAuthorized,
  isPublicPath,
  type TokenAuthConfig,
  UNAUTHORIZED_BODY,
  UNAUTHORIZED_STATUS,
  WEBSOCKET_CLOSE_CODE,
} from "./index.ts";

interface ManagerAuthGolden {
  secret: string;
  operator_token: string;
  fleet_token: string;
  derived: Array<{ name: string; secret: string; agent_id: string; token: string }>;
  routes: Array<{ name: string; method: string; path: string; agent_id: string | null }>;
  authorized: Array<{
    enforce_per_agent: boolean;
    has_secret: boolean;
    token: string;
    target: string;
    method: string;
    path: string;
    authorized: boolean;
  }>;
  no_fleet_token: Array<{ token: string; authorized: boolean }>;
  extracted: Array<{
    name: string;
    scope: { type: string; method: string | null; query_string: string; headers: string[][] };
    token: string;
    pass_through: boolean;
  }>;
  requests: Array<{
    name: string;
    scope: {
      type: string;
      method: string | null;
      path: string | null;
      query_string: string;
      headers: string[][];
    };
    reached_inner: boolean;
    sent: Array<Record<string, unknown>>;
  }>;
}

const golden = loadGolden<ManagerAuthGolden>("manager_auth_golden.json");

/** The middleware as the corpus configured it. */
function configFor(hasSecret: boolean, enforce: boolean): TokenAuthConfig {
  return {
    token: golden.operator_token,
    workerToken: golden.fleet_token,
    ...(hasSecret ? { workerSecret: golden.secret } : {}),
    enforcePerAgentWorkerToken: enforce,
  };
}

/** The corpus's token names, resolved to the tokens themselves. */
function tokenNamed(name: string): string {
  const tokens: Record<string, string> = {
    "the operator token": golden.operator_token,
    "the fleet token": golden.fleet_token,
    "this agent's own derived token": deriveAgentToken(golden.secret, "agent-1"),
    "another agent's derived token": deriveAgentToken(golden.secret, "agent-2"),
    "a token derived under the wrong secret": deriveAgentToken("other-secret", "agent-1"),
    "no token at all": "",
    "a token that is nearly the operator's": `${golden.operator_token} `,
    "the operator token in capitals": golden.operator_token.toUpperCase(),
    nothing: "",
    "a derived token": deriveAgentToken(golden.secret, "agent-1"),
  };
  return tokens[name] as string;
}

/** An ASGI scope from the corpus's recorded form. */
function scopeOf(record: ManagerAuthGolden["requests"][number]["scope"]): AsgiScope {
  return {
    type: record.type,
    ...(record.method === null ? {} : { method: record.method }),
    ...(record.path === null ? {} : { path: record.path }),
    queryString: record.query_string,
    headers: record.headers.map(([name, value]) => [name as string, value as string] as [string, string]),
  };
}

describe("deriving a worker's own token", () => {
  it.each(golden.derived)("$name", (record) => {
    expect(deriveAgentToken(record.secret, record.agent_id)).toBe(record.token);
  });

  it("is one-way, so a worker cannot compute another's", () => {
    // The whole reason the secret never leaves the manager: a worker holding
    // HMAC(secret, its own id) learns nothing about HMAC(secret, any other).
    const mine = deriveAgentToken(golden.secret, "agent-1");
    const theirs = deriveAgentToken(golden.secret, "agent-2");
    expect(mine).not.toBe(theirs);
    expect(deriveAgentToken(mine, "agent-2")).not.toBe(theirs);
  });

  it("names the algorithm in the token, as the wire contract does", () => {
    expect(deriveAgentToken(golden.secret, "agent-1")).toMatch(/^sha256=[0-9a-f]{64}$/);
  });

  it("changes completely for a one-character difference in the id", () => {
    const a = deriveAgentToken(golden.secret, "agent-1");
    const b = deriveAgentToken(golden.secret, "agent-2");
    expect(a.slice(7, 15)).not.toBe(b.slice(7, 15));
  });
});

describe("which routes a worker may reach", () => {
  it.each(golden.routes)("$name", (record) => {
    expect(extractSelfReportAgentId(record.path, record.method) ?? null).toBe(record.agent_id);
  });

  it("matches the whole path and nothing less", () => {
    // A near-miss reaching the low-privilege branch would hand a worker token
    // a route it was never meant to have.
    for (const path of [
      "/agent/agent-1/statusfoo",
      "/agent/agent-1/xstatus",
      "/agent/a/b/status",
      "/agent/agent-1/status/",
      "/api/agent/agent-1/status",
      "/agent//status",
      "/agent/agent-1/status?x=1",
    ]) {
      expect(extractSelfReportAgentId(path, "POST")).toBeUndefined();
    }
  });

  it("anchors the register route as tightly as the status one", () => {
    // Both patterns, not just the first: register is the more dangerous of
    // the two, since it is what claims an agent id in the first place.
    for (const path of [
      "/agent/agent-1/registerfoo",
      "/agent/agent-1/xregister",
      "/agent/a/b/register",
      "/agent/agent-1/register/",
      "/api/agent/agent-1/register",
      "/agent//register",
    ]) {
      expect(extractSelfReportAgentId(path, "POST")).toBeUndefined();
    }
  });

  it("takes only the two self-report routes, and only by POST", () => {
    for (const route of ["status", "register"]) {
      expect(extractSelfReportAgentId(`/agent/agent-1/${route}`, "POST")).toBe("agent-1");
      for (const method of ["GET", "PUT", "DELETE", "PATCH", "post"]) {
        expect(extractSelfReportAgentId(`/agent/agent-1/${route}`, method)).toBeUndefined();
      }
    }
  });

  it("gives an operator route no agent id at all", () => {
    for (const path of ["/spawn", "/agent/agent-1/kill", "/agents", "/", ""]) {
      expect(extractSelfReportAgentId(path, "POST")).toBeUndefined();
    }
  });
});

describe("who may call what", () => {
  it.each(golden.authorized)("$token on $target (enforce=$enforce_per_agent, secret=$has_secret)", (record) => {
    const config = configFor(record.has_secret, record.enforce_per_agent);
    expect(isAuthorized(config, tokenNamed(record.token), record.path, record.method)).toBe(record.authorized);
  });

  it.each(golden.no_fleet_token)("with no fleet token: $token", (record) => {
    expect(
      isAuthorized({ token: golden.operator_token }, tokenNamed(record.token), "/agent/agent-1/status", "POST"),
    ).toBe(record.authorized);
  });

  it("binds a worker's token to the agent id in the path", () => {
    // The property the whole scheme exists for: one compromised worker
    // cannot report as, or register over, any other.
    const config = configFor(true, true);
    const mine = deriveAgentToken(golden.secret, "agent-1");
    expect(isAuthorized(config, mine, "/agent/agent-1/status", "POST")).toBe(true);
    expect(isAuthorized(config, mine, "/agent/agent-2/status", "POST")).toBe(false);
    expect(isAuthorized(config, mine, "/agent/agent-1/register", "POST")).toBe(true);
    expect(isAuthorized(config, mine, "/agent/agent-2/register", "POST")).toBe(false);
  });

  it("never lets a worker token reach an operator route", () => {
    for (const enforce of [false, true]) {
      const config = configFor(true, enforce);
      for (const token of [golden.fleet_token, deriveAgentToken(golden.secret, "agent-1")]) {
        expect(isAuthorized(config, token, "/spawn", "POST")).toBe(false);
        expect(isAuthorized(config, token, "/agent/agent-1/kill", "POST")).toBe(false);
        expect(isAuthorized(config, token, "/agents", "GET")).toBe(false);
      }
    }
  });

  it("lets the fleet token stand in for any agent until enforcement is on", () => {
    // The backward-compatible hole, kept deliberately and closed by the
    // setting: a shared token cannot be bound to one agent.
    expect(isAuthorized(configFor(true, false), golden.fleet_token, "/agent/agent-9/status", "POST")).toBe(true);
    expect(isAuthorized(configFor(true, true), golden.fleet_token, "/agent/agent-9/status", "POST")).toBe(false);
  });

  it("gives the operator token every route", () => {
    for (const enforce of [false, true]) {
      const config = configFor(true, enforce);
      for (const [path, method] of [
        ["/spawn", "POST"],
        ["/agents", "GET"],
        ["/agent/agent-1/status", "POST"],
        ["/agent/agent-9/kill", "POST"],
      ] as const) {
        expect(isAuthorized(config, golden.operator_token, path, method)).toBe(true);
      }
    }
  });

  it("refuses a token that merely resembles the operator's", () => {
    const config = configFor(true, true);
    for (const token of [
      `${golden.operator_token} `,
      ` ${golden.operator_token}`,
      golden.operator_token.toUpperCase(),
      golden.operator_token.slice(0, -1),
      `${golden.operator_token}x`,
      "",
    ]) {
      expect(isAuthorized(config, token, "/spawn", "POST")).toBe(false);
    }
  });

  it("refuses everybody when no secret and no fleet token are configured", () => {
    const config: TokenAuthConfig = { token: golden.operator_token };
    expect(isAuthorized(config, deriveAgentToken(golden.secret, "agent-1"), "/agent/agent-1/status", "POST")).toBe(
      false,
    );
    expect(isAuthorized(config, golden.fleet_token, "/agent/agent-1/status", "POST")).toBe(false);
  });
});

describe("finding the token on a request", () => {
  it.each(golden.extracted)("$name", (record) => {
    const scope: AsgiScope = {
      type: record.scope.type,
      ...(record.scope.method === null ? {} : { method: record.scope.method }),
      queryString: record.scope.query_string,
      headers: record.scope.headers.map(([name, value]) => [name as string, value as string] as [string, string]),
    };
    expect(extractRequestToken(scope)).toEqual({ token: record.token, passThrough: record.pass_through });
  });

  it("prefers the bearer header over the api-token one", () => {
    expect(
      extractRequestToken({
        type: "http",
        method: "POST",
        headers: [
          ["authorization", "Bearer from-bearer"],
          ["x-api-token", "from-api"],
        ],
      }).token,
    ).toBe("from-bearer");
  });

  it("reads a bearer scheme case-sensitively, as the reference does", () => {
    // A lower-case `bearer` is not the prefix, so the value falls through to
    // the api-token header — which is absent, giving nothing.
    expect(
      extractRequestToken({ type: "http", method: "POST", headers: [["authorization", "bearer abc"]] }).token,
    ).toBe("");
  });

  it("lets a preflight through without a token", () => {
    // A browser cannot attach one to an OPTIONS request.
    expect(extractRequestToken({ type: "http", method: "OPTIONS", headers: [] })).toEqual({
      token: "",
      passThrough: true,
    });
  });

  it("takes a websocket's token from the query, since it has no headers to give", () => {
    expect(extractRequestToken({ type: "websocket", queryString: "token=abc" }).token).toBe("abc");
    expect(extractRequestToken({ type: "websocket", queryString: "token=one&token=two" }).token).toBe("one");
    expect(extractRequestToken({ type: "websocket", queryString: "other=abc" }).token).toBe("");
  });

  it("ignores a header on a websocket, which is where a token would hide", () => {
    expect(
      extractRequestToken({
        type: "websocket",
        queryString: "",
        headers: [["authorization", "Bearer abc"]],
      }).token,
    ).toBe("");
  });

  it("takes a scope with the field simply absent", () => {
    // A scope need not carry every key; the reference reads each with a
    // default, and an absent one must not become a token.
    expect(extractRequestToken({ type: "websocket" })).toEqual({ token: "", passThrough: false });
    expect(extractRequestToken({ type: "http", method: "POST" })).toEqual({ token: "", passThrough: false });
  });

  it("never lets a websocket pass through unchecked", () => {
    // Only an OPTIONS request does, and a websocket has no method.
    expect(extractRequestToken({ type: "websocket", queryString: "" }).passThrough).toBe(false);
  });
});

describe("what a request is allowed to do", () => {
  it.each(golden.requests)("$name", (record) => {
    const decision = authorizeScope(
      {
        token: golden.operator_token,
        workerToken: golden.fleet_token,
        workerSecret: golden.secret,
        publicPaths: new Set(["/health"]),
        publicPrefixes: ["/static/"],
      },
      scopeOf(record.scope),
    );
    expect(decision.allow).toBe(record.reached_inner);
    const sent = decision.allow
      ? []
      : decision.kind === "websocket"
        ? [{ type: "websocket.accept" }, { type: "websocket.close", code: WEBSOCKET_CLOSE_CODE }]
        : [
            { type: "http.response.start", status: UNAUTHORIZED_STATUS },
            { type: "http.response.body", body: UNAUTHORIZED_BODY },
          ];
    expect(sent).toEqual(record.sent);
  });

  it("decides on the method the request actually used", () => {
    // The self-report routes are POST-only, so the same path by GET is an
    // operator read — and a worker token must not reach it.
    const config: TokenAuthConfig = { token: golden.operator_token, workerSecret: golden.secret };
    const worker = deriveAgentToken(golden.secret, "agent-1");
    const scope = (method: string): AsgiScope => ({
      type: "http",
      method,
      path: "/agent/agent-1/status",
      headers: [["authorization", `Bearer ${worker}`]],
    });
    expect(authorizeScope(config, scope("POST")).allow).toBe(true);
    expect(authorizeScope(config, scope("GET")).allow).toBe(false);
    expect(authorizeScope(config, scope("DELETE")).allow).toBe(false);
  });

  it("refuses a worker its own token on an operator route", () => {
    // Which is the point of the split: a taken-over worker cannot spawn.
    const decision = authorizeScope(
      { token: golden.operator_token, workerSecret: golden.secret },
      {
        type: "http",
        method: "POST",
        path: "/spawn",
        headers: [["authorization", `Bearer ${deriveAgentToken(golden.secret, "agent-1")}`]],
      },
    );
    expect(decision).toEqual({ allow: false, kind: "http" });
  });

  it("closes a websocket rather than refusing it outright", () => {
    // A websocket has no status code until it is accepted, so refusing before
    // the handshake would leave the caller with no reason at all.
    const decision = authorizeScope(
      { token: golden.operator_token },
      {
        type: "websocket",
        path: "/ws",
        queryString: "",
      },
    );
    expect(decision).toEqual({ allow: false, kind: "websocket" });
    expect(WEBSOCKET_CLOSE_CODE).toBe(4403);
  });

  it("treats a request with no path at all as naming nothing public", () => {
    // Which refuses it, rather than matching an empty prefix.
    expect(authorizeScope({ token: golden.operator_token }, { type: "http", method: "GET" })).toEqual({
      allow: false,
      kind: "http",
    });
  });

  it("passes anything that is not a request straight through", () => {
    // Lifespan and other ASGI traffic is not somebody calling.
    for (const type of ["lifespan", "unknown"]) {
      expect(authorizeScope({ token: golden.operator_token }, { type })).toEqual({ allow: true });
    }
  });

  it("exempts a public path exactly, and a prefix by prefix", () => {
    const config: TokenAuthConfig = {
      token: golden.operator_token,
      publicPaths: new Set(["/health"]),
      publicPrefixes: ["/static/"],
    };
    expect(isPublicPath(config, "/health")).toBe(true);
    expect(isPublicPath(config, "/healthz")).toBe(false);
    expect(isPublicPath(config, "/health/")).toBe(false);
    expect(isPublicPath(config, "/static/app.js")).toBe(true);
    expect(isPublicPath(config, "/static/")).toBe(true);
    expect(isPublicPath(config, "/static")).toBe(false);
    expect(isPublicPath(config, "/spawn")).toBe(false);
    // A prefix is a prefix: a public segment appearing later in the path is
    // not an exemption, or any route could be reached by naming one.
    expect(isPublicPath(config, "/spawn/static/x")).toBe(false);
    expect(isPublicPath(config, "/agent/a/health")).toBe(false);
  });

  it("exempts nothing when nothing is configured public", () => {
    expect(isPublicPath({ token: golden.operator_token }, "/health")).toBe(false);
  });

  it("says only that it was unauthorized, not why", () => {
    // A caller learning which of the two tokens it failed is a caller being
    // told what to try next.
    expect(UNAUTHORIZED_STATUS).toBe(401);
    expect(JSON.parse(UNAUTHORIZED_BODY)).toEqual({ error: "Unauthorized" });
  });
});
