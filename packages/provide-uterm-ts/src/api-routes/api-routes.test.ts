//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { pyReEscape } from "../pycompat/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  API_ROUTE_REGISTRY,
  API_ROUTES,
  HTTP_METHODS,
  type HttpMethod,
  ROUTE_SCOPES,
  type RouteDef,
  RouteDefinitionError,
  RouteRegistry,
  type RouteScope,
} from "./index.ts";

interface Raised {
  error: string | null;
  message: string | null;
}

interface RoutesGolden {
  methods: string[];
  scopes: string[];
  routes: Array<{
    operation: string;
    method: string;
    template: string;
    scope: string;
    capability: string;
    roles: string[];
  }>;
  capabilities: string[];
  matches: Array<{
    method: string;
    path: string;
    operation: string | null;
    params: Record<string, string> | null;
  }>;
  allowed: Array<{ path: string; methods: string[] }>;
  templates: Array<{ name: string; template: string } & Raised>;
  intersections: Array<{ name: string; first: string; second: string } & Raised>;
  custom_templates: string[];
  custom_matches: Array<{ path: string; operation: string | null; params: Record<string, string> | null }>;
  session_without_parameter: Raised;
  session_with_parameter: Raised;
  session_with_wrong_parameter: Raised;
  blank_operation: Raised;
  padded_operation: Raised;
  blank_capability: Raised;
  padded_capability: Raised;
  duplicate: Raised;
  same_template_two_methods: Raised;
  empty_registry: Raised;
  capabilities_all: Raised;
  capabilities_missing: Raised;
  capabilities_none: Raised;
  capabilities_extra: Raised;
  roles_from_frozenset: string[];
  roles_from_list: string[];
}

const golden = loadGolden<RoutesGolden>("apiroutes_golden.json");

/** One route, as the corpus describes it. */
function route(template: string, scope: RouteScope = "global", operation = "a", capability = "a"): RouteDef {
  return { operation, method: "GET", template, scope, capability, roles: [] };
}

/** What a call refuses, in the shape the corpus records. */
function raised(call: () => unknown): Raised {
  try {
    call();
  } catch (error) {
    // The reference raises `ValueError`; there is no such type here, so the
    // corpus's name is mapped rather than compared directly.
    return {
      error: error instanceof RouteDefinitionError ? "ValueError" : (error as Error).constructor.name,
      message: (error as Error).message,
    };
  }
  return { error: null, message: null };
}

describe("the shared contract", () => {
  it("has the methods both backends understand", () => {
    expect([...HTTP_METHODS]).toEqual(golden.methods);
    expect([...ROUTE_SCOPES]).toEqual(golden.scopes);
  });

  it("has every route the reference has, identically", () => {
    // The whole point of the table: the two backends dispatch from the same
    // inventory, so a client can reach either without knowing which. The
    // committed table was transcribed from this corpus once, so this cannot
    // have failed on the day it was written — its job is drift, and it fails
    // the moment a route is added, renamed or rescoped on either side.
    expect(API_ROUTES.map((entry) => ({ ...entry, roles: [...entry.roles] }))).toEqual(golden.routes);
  });

  it("names each capability exactly once per operation", () => {
    expect([...new Set(API_ROUTES.map((entry) => entry.capability))].sort()).toEqual(golden.capabilities);
  });

  it("builds without complaint", () => {
    // Construction validates, so the table shipping at all is the assertion.
    expect(API_ROUTE_REGISTRY.routes).toHaveLength(golden.routes.length);
  });

  it("gives every session route a session to act on", () => {
    for (const entry of API_ROUTES) {
      if (entry.scope === "session") {
        expect(entry.template).toContain("{session_id}");
      }
    }
  });
});

describe("matching a request", () => {
  it.each(golden.matches)("$method $path", (record) => {
    const found = API_ROUTE_REGISTRY.match(record.method, record.path);
    expect(found?.route.operation ?? null).toBe(record.operation);
    expect(found === undefined ? null : { ...found.params }).toEqual(record.params);
  });

  it("extracts the parameters the handler needs", () => {
    const found = API_ROUTE_REGISTRY.match("DELETE", "/api/sessions/w1/webhooks/wh1");
    expect(found?.route.operation).toBe("sessions.webhooks.delete");
    expect({ ...found?.params }).toEqual({ session_id: "w1", webhook_id: "wh1" });
  });

  it("accepts a method in any case", () => {
    // What arrives off the wire is not normalised for us.
    for (const method of ["get", "GeT", "GET"]) {
      expect(API_ROUTE_REGISTRY.match(method, "/api/sessions")?.route.operation).toBe("sessions.list");
    }
  });

  it("refuses a method that is not one", () => {
    // Not an error: an unknown verb is simply no route, which the caller
    // turns into a 404 or a 405 by asking what is allowed.
    expect(API_ROUTE_REGISTRY.match("BREW", "/api/sessions")).toBeUndefined();
    expect(API_ROUTE_REGISTRY.match("", "/api/sessions")).toBeUndefined();
  });

  it("refuses a method the contract has no route for", () => {
    expect(API_ROUTE_REGISTRY.match("HEAD", "/api/sessions")).toBeUndefined();
    expect(API_ROUTE_REGISTRY.match("OPTIONS", "/api/sessions")).toBeUndefined();
  });

  it("matches a path in full or not at all", () => {
    // A prefix match would route `/api/sessions/w1/nope` to the session
    // handler, which would then act on a request it does not implement.
    expect(API_ROUTE_REGISTRY.match("GET", "/api/sessions/w1/nope")).toBeUndefined();
    expect(API_ROUTE_REGISTRY.match("GET", "api/sessions")).toBeUndefined();
    expect(API_ROUTE_REGISTRY.match("GET", "/api/sessions/w1/")).toBeUndefined();
  });

  it("matches the path case-sensitively", () => {
    // Unlike the method. `/API/sessions` is a different path, not a shouted
    // one.
    expect(API_ROUTE_REGISTRY.match("GET", "/API/sessions")).toBeUndefined();
  });

  it("does not let a newline end the path early", () => {
    // A matcher anchored with `$` in multiline mode would accept the first
    // of these, routing a request whose path carries an injected line.
    expect(API_ROUTE_REGISTRY.match("GET", "/api/sessions/w1\n")).toBeUndefined();
    expect(API_ROUTE_REGISTRY.match("GET", "/api/sessions/w1\nx")).toBeUndefined();
    expect(API_ROUTE_REGISTRY.match("GET", "\n/api/sessions/w1")).toBeUndefined();
  });
});

describe("what a parameter may hold", () => {
  it("takes one segment of a restricted alphabet", () => {
    expect(API_ROUTE_REGISTRY.match("GET", "/api/sessions/w-1_2")?.params.session_id).toBe("w-1_2");
  });

  it("refuses a segment that is not that alphabet", () => {
    // Not a match with a strange value — no match at all, which is what lets
    // the Worker tell a bad parameter from an unknown path.
    for (const value of ["a.b", "a~b", "a b", "a%2Fb", "a/b", ""]) {
      expect(API_ROUTE_REGISTRY.match("GET", `/api/sessions/${value}`)).toBeUndefined();
    }
  });

  it("refuses non-ascii digits and full-width letters", () => {
    // A port reaching for `\w` or a Unicode-aware class would take both.
    expect(API_ROUTE_REGISTRY.match("GET", "/api/sessions/٣")).toBeUndefined();
    expect(API_ROUTE_REGISTRY.match("GET", "/api/sessions/ｗ1")).toBeUndefined();
  });

  it("bounds the length", () => {
    // An unbounded parameter is an unbounded key into whatever storage the
    // handler reaches for.
    expect(API_ROUTE_REGISTRY.match("GET", `/api/sessions/${"x".repeat(64)}`)?.route.operation).toBe("sessions.get");
    expect(API_ROUTE_REGISTRY.match("GET", `/api/sessions/${"x".repeat(65)}`)).toBeUndefined();
  });
});

describe("what else the path would accept", () => {
  it.each(golden.allowed)("$path", (record) => {
    expect(API_ROUTE_REGISTRY.allowedMethods(record.path)).toEqual(record.methods);
  });

  it("reports them in a stable order", () => {
    // The value goes into an `Allow` header, so it has to be the same on
    // every request rather than whatever a set iterated to.
    expect(API_ROUTE_REGISTRY.allowedMethods("/api/sessions")).toEqual(["DELETE", "GET", "POST"]);
  });

  it("reports nothing for a path no route has", () => {
    // Which is how the caller tells a 405 from a 404.
    expect(API_ROUTE_REGISTRY.allowedMethods("/api/nope")).toEqual([]);
  });

  it("ignores the method entirely", () => {
    expect(API_ROUTE_REGISTRY.allowedMethods("/api/sessions/w1/connect")).toEqual(["POST"]);
  });
});

describe("validating a template", () => {
  it.each(golden.templates)("$name", (record) => {
    expect(raised(() => new RouteRegistry([route(record.template)]))).toEqual({
      error: record.error,
      message: record.message,
    });
  });

  it("refuses anything outside the api prefix", () => {
    // Every route in this table is an API route; one that is not would be
    // dispatched by a rule nobody wrote.
    expect(() => new RouteRegistry([route("/things")])).toThrow(RouteDefinitionError);
    expect(() => new RouteRegistry([route("/api")])).toThrow(RouteDefinitionError);
  });

  it("refuses a template that could never be a path", () => {
    for (const template of ["/api/things/", "/api//things", "/api/things?x=1", "/api/things#x"]) {
      expect(() => new RouteRegistry([route(template)])).toThrow(RouteDefinitionError);
    }
  });

  it("names the four parameters it knows and no others", () => {
    // A typo'd name would otherwise compile into a group nothing reads, and
    // the handler would look up a parameter that is never there.
    for (const name of ["session_id", "tunnel_id", "profile_id", "webhook_id"]) {
      expect(() => new RouteRegistry([route(`/api/things/{${name}}`)])).not.toThrow();
    }
    expect(() => new RouteRegistry([route("/api/things/{thing_id}")])).toThrow(/invalid route template parameter/);
  });

  it("refuses the same parameter twice", () => {
    // Two groups of one name is a regex error in some engines and a silent
    // last-wins in others.
    expect(() => new RouteRegistry([route("/api/{session_id}/{session_id}")])).toThrow(
      /invalid route template parameter/,
    );
  });

  it("refuses a brace that is not a whole segment", () => {
    for (const template of ["/api/th{ing}s", "/api/{session_id", "/api/session_id}"]) {
      expect(() => new RouteRegistry([route(template)])).toThrow(/invalid route template/);
    }
  });

  it("allows a static segment the wider alphabet", () => {
    // A static segment is written by hand, so it may hold what a parameter
    // may not.
    for (const template of ["/api/things.json", "/api/al~pha", "/api/al-pha", "/api/al_pha"]) {
      expect(() => new RouteRegistry([route(template)])).not.toThrow();
    }
  });

  it("requires a session route to say which session", () => {
    expect(raised(() => new RouteRegistry([route("/api/things", "session")]))).toEqual(
      golden.session_without_parameter,
    );
    expect(raised(() => new RouteRegistry([route("/api/things/{session_id}", "session")]))).toEqual(
      golden.session_with_parameter,
    );
  });

  it("refuses a method or scope that is not one", () => {
    // The type system covers a table written by hand; this covers one that
    // arrived as data, where a typo is a value rather than a compile error.
    expect(() => new RouteRegistry([{ ...route("/api/things"), method: "BREW" as HttpMethod }])).toThrow(
      /route method must be an HttpMethod/,
    );
    expect(() => new RouteRegistry([{ ...route("/api/things"), scope: "worker" as RouteScope }])).toThrow(
      /route scope must be a RouteScope/,
    );
  });

  it("requires the session parameter, not merely a parameter", () => {
    // A session route has to be able to say which session it acts on, and a
    // check for any parameter at all would accept a tunnel id instead.
    expect(raised(() => new RouteRegistry([route("/api/things/{tunnel_id}", "session")]))).toEqual(
      golden.session_with_wrong_parameter,
    );
  });

  it("requires stable metadata", () => {
    // Both are keys a backend looks a handler up by, so a stray space would
    // make one unreachable.
    expect(raised(() => new RouteRegistry([route("/api/things", "global", "")]))).toEqual(golden.blank_operation);
    expect(raised(() => new RouteRegistry([route("/api/things", "global", " a ")]))).toEqual(golden.padded_operation);
    expect(raised(() => new RouteRegistry([route("/api/things", "global", "a", "")]))).toEqual(golden.blank_capability);
    expect(raised(() => new RouteRegistry([route("/api/things", "global", "a", "a\t")]))).toEqual(
      golden.padded_capability,
    );
  });
});

describe("routes that would shadow each other", () => {
  it.each(golden.intersections)("$name", (record) => {
    expect(
      raised(
        () =>
          new RouteRegistry([
            { ...route(record.first), operation: "a", capability: "a" },
            { ...route(record.second), operation: "b", capability: "b" },
          ]),
      ),
    ).toEqual({ error: record.error, message: record.message });
  });

  it("refuses a parameter that could swallow a literal", () => {
    // Allowing both would make dispatch depend on declaration order, so
    // `/api/things/latest` would silently become a session id one day.
    expect(
      () => new RouteRegistry([route("/api/things/{session_id}"), { ...route("/api/things/latest"), operation: "b" }]),
    ).toThrow(/intersecting route/);
  });

  it("allows a literal no parameter could ever equal", () => {
    // The parameter alphabet is stricter than a static segment's, so these
    // cannot collide and refusing them would forbid a legitimate pair.
    expect(
      () =>
        new RouteRegistry([route("/api/things/{session_id}"), { ...route("/api/things/all.json"), operation: "b" }]),
    ).not.toThrow();
    expect(
      () =>
        new RouteRegistry([
          route("/api/things/{session_id}"),
          { ...route(`/api/things/${"x".repeat(65)}`), operation: "b" },
        ]),
    ).not.toThrow();
  });

  it("compares the same pair either way round", () => {
    // Which side holds the parameter decides which arm of the comparison
    // runs, so declaration order must not change the verdict.
    const literal = { ...route("/api/things/latest"), operation: "b", capability: "b" };
    const parameter = route("/api/things/{session_id}");
    expect(() => new RouteRegistry([parameter, literal])).toThrow(/intersecting route/);
    expect(() => new RouteRegistry([literal, parameter])).toThrow(/intersecting route/);

    const dotted = { ...route("/api/things/all.json"), operation: "b", capability: "b" };
    expect(() => new RouteRegistry([parameter, dotted])).not.toThrow();
    expect(() => new RouteRegistry([dotted, parameter])).not.toThrow();
  });

  it("allows templates of different lengths", () => {
    expect(
      () => new RouteRegistry([route("/api/things/{session_id}"), { ...route("/api/things"), operation: "b" }]),
    ).not.toThrow();
  });

  it("reports a duplicate before an intersection", () => {
    // The same method and template twice is the more precise complaint, and
    // an identical pair satisfies both rules.
    expect(
      raised(() => new RouteRegistry([route("/api/things"), { ...route("/api/things"), operation: "b" }])),
    ).toEqual(golden.duplicate);
  });

  it("allows one template under two methods", () => {
    // The ordinary case: a collection that lists and creates.
    expect(
      raised(
        () =>
          new RouteRegistry([
            route("/api/things"),
            { ...route("/api/things"), method: "POST" as HttpMethod, operation: "b", capability: "b" },
          ]),
      ),
    ).toEqual(golden.same_template_two_methods);
  });

  it("accepts an empty table", () => {
    expect(raised(() => new RouteRegistry([]))).toEqual(golden.empty_registry);
  });
});

describe("a static segment using the wider alphabet", () => {
  // The shared table has no example of one, so this needs a registry of its
  // own — and an unescaped dot is exactly the bug that would go unnoticed
  // without it.
  const custom = new RouteRegistry(
    golden.custom_templates.map((template, index) => route(template, "global", `r${index}`, `r${index}`)),
  );

  it.each(golden.custom_matches)("$path", (record) => {
    const found = custom.match("GET", record.path);
    expect(found?.route.operation ?? null).toBe(record.operation);
    expect(found === undefined ? null : { ...found.params }).toEqual(record.params);
  });

  it("matches a dot literally", () => {
    // Compiled unescaped it would match any character, so this path would
    // reach the route for `/api/things.json`.
    expect(custom.match("GET", "/api/things.json")?.route.operation).toBe("r0");
    expect(custom.match("GET", "/api/thingsxjson")).toBeUndefined();
  });

  it("does not let a dot swallow the separator", () => {
    // `/api/things/json` is the parameter route, not the dotted one.
    expect(custom.match("GET", "/api/things/json")?.route.operation).toBe("r3");
  });

  it("escapes exactly what the reference's escaper would", () => {
    // The segment escaper is written locally so this module needs nothing
    // from the Node-facing runtime and the browser SPA can import it. That
    // is only safe while it agrees with the reference across every character
    // a static segment may hold, which the validator has already closed.
    for (const character of "abzABZ019._~-") {
      const template = `/api/x${character}y`;
      const registry = new RouteRegistry([route(template)]);
      // Escaped or not, the segment matches itself and nothing that merely
      // looks like it.
      expect(registry.match("GET", template)?.route.operation).toBe("a");
      expect(registry.match("GET", `/api/xQy`)).toBeUndefined();
      expect(pyReEscape(`x${character}y`)).toBe(`x${character}y`.replace(/[^A-Za-z0-9_]/g, "\\$&"));
    }
  });

  it("matches a tilde and a dash literally", () => {
    expect(custom.match("GET", "/api/al~pha")?.route.operation).toBe("r1");
    expect(custom.match("GET", "/api/alxpha")).toBeUndefined();
    expect(custom.match("GET", "/api/al-pha")?.route.operation).toBe("r2");
    expect(custom.match("GET", "/api/al_pha")).toBeUndefined();
  });
});

describe("a backend that cannot serve the table", () => {
  it("passes when it serves everything", () => {
    expect(raised(() => API_ROUTE_REGISTRY.validateCapabilities(API_ROUTES.map((entry) => entry.capability)))).toEqual(
      golden.capabilities_all,
    );
  });

  it("names what is missing, in order", () => {
    // The message is read by a person wiring a backend up, so the list is
    // sorted rather than in whatever order a set produced.
    expect(raised(() => API_ROUTE_REGISTRY.validateCapabilities(["sessions.list"]))).toEqual(
      golden.capabilities_missing,
    );
    expect(raised(() => API_ROUTE_REGISTRY.validateCapabilities([]))).toEqual(golden.capabilities_none);
  });

  it("does not mind a backend that serves more", () => {
    // A backend with its own operations beyond the shared contract is fine;
    // only a gap is a problem.
    expect(
      raised(() => API_ROUTE_REGISTRY.validateCapabilities([...API_ROUTES.map((e) => e.capability), "extra"])),
    ).toEqual(golden.capabilities_extra);
  });
});

describe("the roles a route requires", () => {
  it("is a list whatever it was given", () => {
    // The reference normalises a frozenset or a list to a tuple; here the
    // corpus pins what those become.
    expect(golden.roles_from_frozenset).toEqual(["admin"]);
    expect(golden.roles_from_list).toEqual(["admin", "operator"]);
  });

  it("guards the two operations that need it", () => {
    const guarded = API_ROUTES.filter((entry) => entry.roles.length > 0);
    expect(guarded.map((entry) => [entry.operation, [...entry.roles]])).toEqual([
      ["sessions.bulk_delete", ["admin"]],
      ["pam_events.ingest", ["operator", "admin"]],
    ]);
  });

  it("leaves every other route to the capability check", () => {
    expect(API_ROUTES.filter((entry) => entry.roles.length === 0)).toHaveLength(API_ROUTES.length - 2);
  });
});
