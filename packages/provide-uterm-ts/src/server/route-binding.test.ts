//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { API_ROUTES, type RouteDef } from "../api-routes/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { bindApiRoutes, RouteBindingError, type RouteHandler, routeGuard } from "./index.ts";

interface BindingGolden {
  bindings: Array<{
    name: string;
    operations: string[];
    with_authorizer: boolean;
    missing: string[];
    registered: Array<{ template: string; methods: string[]; name: string | null; in_schema: boolean }>;
    error: string | null;
    message: string | null;
  }>;
  foreign_route: { error: string | null; message: string | null };
  guards: Array<{
    name: string;
    operation: string;
    path: string;
    authorized: boolean | null;
    status: number | null;
    detail: string | null;
  }>;
  all_methods: string[];
}

const golden = loadGolden<BindingGolden>("routebinding_golden.json");

/** The shared routes named, in the table's own order. */
function byOperation(...operations: string[]): RouteDef[] {
  return API_ROUTES.filter((route) => operations.includes(route.operation));
}

/** A handler for every capability in the shared table, less any dropped. */
function handlers(missing: readonly string[] = []): Map<string, RouteHandler> {
  const map = new Map<string, RouteHandler>();
  for (const route of API_ROUTES) {
    if (!missing.includes(route.capability)) {
      map.set(route.capability, async () => new Response("ok"));
    }
  }
  return map;
}

/** What a binding refuses, in the shape the corpus records. */
function raised(call: () => unknown): { error: string | null; message: string | null } {
  try {
    call();
  } catch (error) {
    return {
      // The reference raises `ValueError`; there is no such type here.
      error: error instanceof RouteBindingError ? "ValueError" : (error as Error).constructor.name,
      message: (error as Error).message,
    };
  }
  return { error: null, message: null };
}

describe("binding routes to handlers", () => {
  it.each(golden.bindings)("$name", (record) => {
    const outcome = raised(() =>
      bindApiRoutes(handlers(record.missing), byOperation(...record.operations), {
        ...(record.with_authorizer ? { roleAuthorizer: () => true } : {}),
      }),
    );
    expect(outcome).toEqual({ error: record.error, message: record.message });
  });

  it("refuses a backend that cannot serve the whole contract", () => {
    // Not merely the routes it selected. A backend serving part of the
    // contract cannot register the part it has: what did bind would answer,
    // and what did not would 404 as though it never existed.
    expect(() => bindApiRoutes(handlers(["tunnels.create"]), byOperation("sessions.get"))).toThrow(
      /missing route capabilities: tunnels.create/,
    );
  });

  it("registers nothing when it refuses", () => {
    // Checked before anything is bound, so a failed adapter leaves no half-
    // registered router behind.
    expect(() => bindApiRoutes(handlers(["sessions.connect"]), byOperation("sessions.get"))).toThrow(RouteBindingError);
  });

  it("refuses a route the shared table does not have", () => {
    const foreign: RouteDef = {
      operation: "made.up",
      method: "GET",
      template: "/api/made-up",
      scope: "global",
      capability: "made.up",
      roles: [],
    };
    expect(raised(() => bindApiRoutes(handlers(), [foreign]))).toEqual(golden.foreign_route);
  });

  it("refuses a guarded route with nothing to guard it", () => {
    // Publishing it unguarded is the one mistake this layer exists to
    // prevent.
    expect(() => bindApiRoutes(handlers(), byOperation("sessions.bulk_delete"))).toThrow(/role_authorizer is required/);
    expect(() =>
      bindApiRoutes(handlers(), byOperation("sessions.bulk_delete"), { roleAuthorizer: () => true }),
    ).not.toThrow();
  });

  it("accepts an empty selection", () => {
    expect(() => bindApiRoutes(handlers(), [])).not.toThrow();
  });
});

describe("what a bound router answers", () => {
  /** A router bound to the named operations. */
  function bind(operations: string[], authorized?: boolean) {
    return bindApiRoutes(handlers(), byOperation(...operations), {
      ...(authorized === undefined ? {} : { roleAuthorizer: () => authorized }),
    });
  }

  it.each(golden.guards)("$name", async (record) => {
    // The guard on its own, which is what the corpus records: in the
    // reference it runs after the framework has already matched the
    // template, so every path that is not this route reads as bad
    // parameters. The dispatcher below reaches its own conclusions first.
    const route = byOperation(record.operation)[0] as RouteDef;
    const response = await routeGuard(
      route,
      record.path,
      record.authorized === null ? undefined : () => record.authorized as boolean,
    );
    if (record.status === null) {
      expect(response).toBeUndefined();
      return;
    }
    expect(response?.status).toBe(record.status);
    expect(await (response as Response).json()).toEqual({ detail: record.detail });
  });

  it("awaits a guard authorizer that answers asynchronously", async () => {
    const route = byOperation("sessions.bulk_delete")[0] as RouteDef;
    expect((await routeGuard(route, "/api/sessions", async () => false))?.status).toBe(403);
    expect(await routeGuard(route, "/api/sessions", async () => true)).toBeUndefined();
  });

  it("answers 404 where the guard alone would say 422", async () => {
    // The one place this layer differs from the reference, and it differs
    // because it does more: the framework would never have routed `/nope` to
    // this route, so its guard never sees it. A dispatcher that does its own
    // matching has to tell "not here" from "asked wrongly".
    const router = bind(["sessions.get"]);
    expect((await router.dispatch("GET", "/nope")).status).toBe(404);
    expect((await routeGuard(byOperation("sessions.get")[0] as RouteDef, "/nope"))?.status).toBe(422);
  });

  it("calls the handler for the operation the path names", async () => {
    const seen: string[] = [];
    const map = handlers();
    map.set("sessions.get", async (context) => {
      seen.push(String(context.params.session_id));
      return new Response("body");
    });
    const router = bindApiRoutes(map, byOperation("sessions.get"));
    expect(await (await router.dispatch("GET", "/api/sessions/w1")).text()).toBe("body");
    expect(seen).toEqual(["w1"]);
  });

  it("tells a bad parameter from an unknown path", async () => {
    // The route exists; the parameters are wrong. A 404 would say the
    // operation does not exist, which would send a client looking for a
    // different endpoint.
    const router = bind(["sessions.get"]);
    expect((await router.dispatch("GET", "/api/sessions/a.b")).status).toBe(422);
    expect((await router.dispatch("GET", "/api/nowhere")).status).toBe(404);
  });

  it("tells the handler what was asked of it", async () => {
    const seen: Array<{ method: string; path: string; operation: string }> = [];
    const map = handlers();
    map.set("sessions.set_mode", async (context) => {
      seen.push({ method: context.method, path: context.path, operation: context.route.operation });
      return new Response("ok");
    });
    const router = bindApiRoutes(map, byOperation("sessions.set_mode"));
    await router.dispatch("POST", "/api/sessions/w1/mode");
    expect(seen).toEqual([{ method: "POST", path: "/api/sessions/w1/mode", operation: "sessions.set_mode" }]);
  });

  it("does not read a shorter path as a route with a missing parameter", async () => {
    // `/api/sessions` is two segments where the bound route has three. A
    // shape check that ignored the length would read the absent third as an
    // empty parameter and answer 422 for a path this backend simply does not
    // serve.
    const router = bind(["sessions.get"]);
    expect((await router.dispatch("GET", "/api/sessions")).status).toBe(404);
  });

  it("does not read an empty parameter as a present one", async () => {
    const router = bind(["sessions.get"]);
    expect((await router.dispatch("GET", "/api/sessions/")).status).toBe(404);
  });

  it("requires the static segments to match before calling it a bad parameter", async () => {
    // Same length, different path: that is another endpoint, not this one
    // asked wrongly.
    const router = bind(["sessions.get"]);
    expect((await router.dispatch("GET", "/api/nowhere/x")).status).toBe(404);
  });

  it("judges the shape against what it bound, not the whole table", async () => {
    // The shared table has a route shaped like `/api/profiles/{id}`; a
    // backend that did not bind it must say so, not report a parameter
    // problem on somebody else's endpoint.
    const router = bind(["sessions.get"]);
    expect((await router.dispatch("GET", "/api/profiles/p1")).status).toBe(404);
  });

  it("reports every method a path accepts", async () => {
    // A router emitting its own 405 from the first partial match would name
    // only that one method, so a client would retry with the wrong verb.
    const router = bind(["sessions.list", "sessions.create", "sessions.bulk_delete"], true);
    const response = await router.dispatch("PATCH", "/api/sessions");
    expect(response.status).toBe(405);
    expect(response.headers.get("Allow")).toBe("DELETE, GET, POST");
  });

  it("reports only the methods it bound, not the whole table", async () => {
    // The shared table has three methods on that path; a backend that bound
    // one must not advertise the other two.
    const router = bind(["sessions.list"]);
    const response = await router.dispatch("POST", "/api/sessions");
    expect(response.status).toBe(405);
    expect(response.headers.get("Allow")).toBe("GET");
  });

  it("refuses a principal without the role the route requires", async () => {
    const allowed = bind(["sessions.bulk_delete"], true);
    const refused = bind(["sessions.bulk_delete"], false);
    expect((await allowed.dispatch("DELETE", "/api/sessions")).status).toBe(200);
    expect((await refused.dispatch("DELETE", "/api/sessions")).status).toBe(403);
  });

  it("does not consult the authorizer for an unguarded route", async () => {
    // Only routes that declare roles are role-checked; running the authorizer
    // on the rest would let a role check leak onto operations that never
    // asked for one.
    let asked = 0;
    const router = bindApiRoutes(handlers(), byOperation("sessions.get"), {
      roleAuthorizer: () => {
        asked += 1;
        return false;
      },
    });
    expect((await router.dispatch("GET", "/api/sessions/w1")).status).toBe(200);
    expect(asked).toBe(0);
  });

  it("passes the route's required roles to the authorizer", async () => {
    const seen: string[][] = [];
    const router = bindApiRoutes(handlers(), byOperation("sessions.bulk_delete"), {
      roleAuthorizer: (_context, roles) => {
        seen.push([...roles]);
        return true;
      },
    });
    await router.dispatch("DELETE", "/api/sessions");
    expect(seen).toEqual([["admin"]]);
  });

  it("awaits an authorizer that answers asynchronously", async () => {
    // Deciding a role usually means reading a token, which is not
    // synchronous.
    const router = bindApiRoutes(handlers(), byOperation("sessions.bulk_delete"), {
      roleAuthorizer: async () => false,
    });
    expect((await router.dispatch("DELETE", "/api/sessions")).status).toBe(403);
  });

  it("refuses a method that is not a method at all", async () => {
    const router = bind(["sessions.get"]);
    expect((await router.dispatch("BREW", "/api/sessions/w1")).status).toBe(405);
  });
});
