//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { API_ROUTE_REGISTRY, API_ROUTES } from "../api-routes/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  API_PREFIX,
  dispatchApiRoute,
  matchesRouteShape,
  rolesSatisfyRoute,
  validateGlobalCapabilities,
} from "./index.ts";

interface DispatchGolden {
  global_capabilities: string[];
  session_capabilities: string[];
  requests: Array<{
    name: string;
    method: string;
    path: string;
    outcome: string;
    status?: number;
    allow?: string[];
    capability?: string;
    scope?: string;
    roles?: string[];
    params?: Record<string, string>;
  }>;
  shapes: Array<{ name: string; path: string; matches: string[] }>;
}

const golden = loadGolden<DispatchGolden>("apidispatch_golden.json");

/** Every capability the recorded Worker had a handler for. */
const HANDLERS = Object.fromEntries(golden.global_capabilities.map((capability) => [capability, () => undefined]));

describe("what an API request is answered with", () => {
  it.each(golden.requests)("$name", (record) => {
    const outcome = dispatchApiRoute(record.method, record.path, API_ROUTE_REGISTRY);
    expect(outcome.outcome).toBe(record.outcome);
    if (outcome.outcome === "matched") {
      expect(outcome).toEqual({
        outcome: "matched",
        capability: record.capability,
        scope: record.scope,
        roles: record.roles,
        params: record.params,
      });
      return;
    }
    if (outcome.outcome === "method_not_allowed") {
      expect([...outcome.allow]).toEqual(record.allow);
      expect(outcome.status).toBe(405);
      return;
    }
    if (outcome.outcome !== "not_api") {
      expect(outcome.status).toBe(record.status);
    }
  });

  it("says which methods a route does take", () => {
    // A caller that guessed the verb deserves to be told, rather than left to
    // conclude the route does not exist.
    const outcome = dispatchApiRoute("PUT", "/api/sessions", API_ROUTE_REGISTRY);
    expect(outcome).toMatchObject({ outcome: "method_not_allowed", status: 405 });
    expect((outcome as { allow: readonly string[] }).allow).toContain("GET");
    expect((outcome as { allow: readonly string[] }).allow).toContain("POST");
  });

  it("tells a mistyped id apart from a route that does not exist", () => {
    // 422 says "you spelled the id wrong"; 404 says "there is no such
    // endpoint". Collapsing them would hide a caller's real mistake.
    for (const path of ["/api/sessions/a.b", "/api/sessions/a b", "/api/sessions/a%2Fb", "/api/sessions/a!b"]) {
      expect(dispatchApiRoute("GET", path, API_ROUTE_REGISTRY)).toEqual({
        outcome: "invalid_route_parameter",
        status: 422,
      });
    }
    for (const path of ["/api/nowhere", "/api/nowhere/sess-1", "/api/sessions/../x"]) {
      expect(dispatchApiRoute("GET", path, API_ROUTE_REGISTRY)).toEqual({ outcome: "not_found", status: 404 });
    }
  });

  it("checks the method before the parameter", () => {
    // A bad id on a verb the route lacks is still a bad id: the registry
    // reports no allowed methods for a path it cannot match at all.
    expect(dispatchApiRoute("PUT", "/api/sessions/a.b", API_ROUTE_REGISTRY)).toEqual({
      outcome: "invalid_route_parameter",
      status: 422,
    });
  });

  it("leaves a path that is not an API path alone", () => {
    // Answering it here would take it away from the routes that own it.
    for (const path of ["/app/session/sess-1", "/", "/assets/app.js", "/ws/browser/sess-1/term"]) {
      expect(dispatchApiRoute("GET", path, API_ROUTE_REGISTRY)).toEqual({ outcome: "not_api" });
    }
    expect(API_PREFIX).toBe("/api/");
  });

  it("leaves folding the verb to the registry", () => {
    // Which folds it: doing it twice would read as though one of the two were
    // load-bearing.
    expect(dispatchApiRoute("get", "/api/sessions", API_ROUTE_REGISTRY)).toEqual(
      dispatchApiRoute("GET", "/api/sessions", API_ROUTE_REGISTRY),
    );
  });

  it("reads a lower-case verb as the verb it is", () => {
    expect(dispatchApiRoute("get", "/api/sessions", API_ROUTE_REGISTRY)).toMatchObject({
      outcome: "matched",
      capability: "sessions.list",
    });
  });

  it("does not fold the path the way it folds the verb", () => {
    // `/API/sessions` is a different path, not a shouted one — and a session
    // id keeps its capitals, because it names an object.
    expect(dispatchApiRoute("GET", "/api/sessions/Sess-1", API_ROUTE_REGISTRY)).toMatchObject({
      params: { session_id: "Sess-1" },
    });
    expect(dispatchApiRoute("GET", "/API/sessions", API_ROUTE_REGISTRY)).toEqual({ outcome: "not_api" });
  });

  it("hands back the parameters it matched", () => {
    expect(dispatchApiRoute("GET", "/api/sessions/sess-1", API_ROUTE_REGISTRY)).toMatchObject({
      params: { session_id: "sess-1" },
    });
  });
});

describe("whether a path has a route's shape", () => {
  it.each(golden.shapes)("$name", (record) => {
    const matched = API_ROUTES.filter((route) => matchesRouteShape(record.path, route.template))
      .map((route) => route.template)
      .sort();
    expect([...new Set(matched)]).toEqual([...new Set(record.matches)]);
  });

  it("counts segments before it compares them", () => {
    expect(matchesRouteShape("/api/sessions", "/api/sessions")).toBe(true);
    expect(matchesRouteShape("/api/sessions/x", "/api/sessions")).toBe(false);
    expect(matchesRouteShape("/api/sessions", "/api/sessions/{session_id}")).toBe(false);
  });

  it("takes any non-empty segment for a parameter", () => {
    expect(matchesRouteShape("/api/sessions/anything", "/api/sessions/{session_id}")).toBe(true);
    expect(matchesRouteShape("/api/sessions/", "/api/sessions/{session_id}")).toBe(false);
  });

  it("compares a literal exactly", () => {
    expect(matchesRouteShape("/api/session/x", "/api/sessions/{session_id}")).toBe(false);
    expect(matchesRouteShape("/api/sessions/x/hijack", "/api/sessions/{session_id}/hijack")).toBe(true);
    expect(matchesRouteShape("/api/sessions/x/hijacks", "/api/sessions/{session_id}/hijack")).toBe(false);
  });
});

describe("whether a caller's roles satisfy a route", () => {
  it("lets any authenticated caller through a route naming none", () => {
    // Authentication has already run, so an empty list means *any
    // authenticated caller* and never *anyone*.
    expect(rolesSatisfyRoute({ roles: [] }, [])).toBe(true);
    expect(rolesSatisfyRoute({ roles: [] }, ["viewer"])).toBe(true);
  });

  it("treats named roles as alternatives, not requirements", () => {
    expect(rolesSatisfyRoute({ roles: ["admin", "operator"] }, ["operator"])).toBe(true);
    expect(rolesSatisfyRoute({ roles: ["admin", "operator"] }, ["admin"])).toBe(true);
    expect(rolesSatisfyRoute({ roles: ["admin", "operator"] }, ["viewer"])).toBe(false);
    expect(rolesSatisfyRoute({ roles: ["admin"] }, [])).toBe(false);
  });

  it("matches a role exactly", () => {
    expect(rolesSatisfyRoute({ roles: ["admin"] }, ["Admin"])).toBe(false);
    expect(rolesSatisfyRoute({ roles: ["admin"] }, ["administrator"])).toBe(false);
  });
});

describe("the capability table the Worker starts with", () => {
  it("has a handler for every global route", () => {
    // A route added without one should stop the Worker starting rather than
    // 500 at request time.
    expect(() => validateGlobalCapabilities(HANDLERS)).not.toThrow();
  });

  it("names what is missing when one has none", () => {
    const { "sessions.list": _dropped, ...incomplete } = HANDLERS;
    expect(() => validateGlobalCapabilities(incomplete)).toThrow("missing Worker route capabilities: sessions.list");
  });

  it("names several in a fixed order", () => {
    // Sorted, so two deployments missing the same handlers read the same
    // message rather than one in registry order.
    const { "tunnels.create": _a, "profiles.list": _b, "sessions.list": _c, ...incomplete } = HANDLERS;
    expect(() => validateGlobalCapabilities(incomplete)).toThrow(
      "missing Worker route capabilities: profiles.list, sessions.list, tunnels.create",
    );
  });

  it("refuses a session capability registered in the Worker", () => {
    // Worse than a missing one: it would answer from the Worker instead of
    // the session's own Durable Object, which is where its state lives.
    const sessionCapability = golden.session_capabilities[0] as string;
    expect(() => validateGlobalCapabilities({ ...HANDLERS, [sessionCapability]: () => undefined })).toThrow(
      "session RouteDef capability registered in Worker",
    );
  });

  it("knows the same two sets the reference knows", () => {
    const globals = [...new Set(API_ROUTES.filter((r) => r.scope === "global").map((r) => r.capability))].sort();
    const sessions = [...new Set(API_ROUTES.filter((r) => r.scope === "session").map((r) => r.capability))].sort();
    expect(globals).toEqual(golden.global_capabilities);
    expect(sessions).toEqual(golden.session_capabilities);
  });

  it("keeps the two sets disjoint", () => {
    // Which is what makes the check above meaningful at all.
    for (const capability of golden.global_capabilities) {
      expect(golden.session_capabilities).not.toContain(capability);
    }
  });
});
