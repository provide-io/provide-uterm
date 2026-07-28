//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Framework-neutral definitions for the shared HTTP API surface.
 *
 * Port of the Python module `provide.uterm.api_routes`.
 *
 * The FastAPI server and the Cloudflare Worker dispatch from the same
 * inventory, which is the only reason a client can talk to either without
 * knowing which one it reached. Nothing framework-specific belongs here.
 *
 * Templates are validated when the table is built rather than when a request
 * arrives: a malformed one is a programming error, and finding it at
 * construction means it cannot ship.
 */

import { pyReEscape } from "../pycompat/index.ts";

/** The HTTP methods the shared contract uses. */
export const HTTP_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"] as const;

/** One of {@link HTTP_METHODS}. */
export type HttpMethod = (typeof HTTP_METHODS)[number];

/** Where a route executes. */
export const ROUTE_SCOPES = ["global", "session"] as const;

/** One of {@link ROUTE_SCOPES}. */
export type RouteScope = (typeof ROUTE_SCOPES)[number];

/**
 * The parameter names a template may use.
 *
 * A closed set rather than anything in braces: a typo would otherwise compile
 * into a capture group nothing reads, and the handler would look up a
 * parameter that is never there.
 */
const PARAMETER_NAMES: ReadonlySet<string> = new Set(["session_id", "tunnel_id", "profile_id", "webhook_id"]);

/**
 * What one path parameter may hold.
 *
 * A single segment of a restricted alphabet, bounded — not a catch-all. An
 * unbounded parameter is an unbounded key into whatever storage the handler
 * reaches for, and one that accepted a slash would let a path claim a route
 * it does not have.
 *
 * Spelled out rather than written `[\w-]`, which is the same class on this
 * engine but not on the reference's — where `\w` is Unicode-aware and would
 * quietly admit `٣` and `ｗ` as session ids.
 */
const PARAMETER_PATTERN = "[A-Za-z0-9_-]{1,64}";

/** A static segment, which is written by hand and may hold more than a parameter. */
const STATIC_SEGMENT = /^[A-Za-z0-9._~-]+$/;

/** A whole parameter segment, matching {@link PARAMETER_PATTERN} end to end. */
const WHOLE_PARAMETER = new RegExp(`^${PARAMETER_PATTERN}$`);

/** A route table that could not be built. Stands in for the reference's `ValueError`. */
export class RouteDefinitionError extends Error {}

/** A single shared API operation, without framework-specific behaviour. */
export interface RouteDef {
  /** The stable name of the operation. */
  operation: string;
  method: HttpMethod;
  /** The path, with parameters in braces. */
  template: string;
  scope: RouteScope;
  /** What a backend must be able to serve to satisfy this route. */
  capability: string;
  /** Roles allowed to call it; empty leaves the decision to the capability. */
  roles: readonly string[];
}

/** A route and the parameters its template pulled out of the path. */
export interface RouteMatch {
  route: RouteDef;
  params: Readonly<Record<string, string>>;
}

/** A route with its compiled matcher. */
interface CompiledRoute {
  route: RouteDef;
  pattern: RegExp;
}

/** Require a stable, nonblank identifier for a route metadata field. */
function requireNormalized(value: string, fieldName: string): void {
  if (value === "" || value !== value.trim()) {
    throw new RouteDefinitionError(`route ${fieldName} must be nonblank and normalized`);
  }
}

/**
 * Validate a template and return its named parameters.
 *
 * A query or a fragment is refused rather than escaped: neither is part of a
 * path, so a template carrying one would never match anything.
 *
 * Only the `/api/` prefix is load-bearing here. A trailing slash, a query, a
 * fragment and an empty segment are each caught again by the per-segment
 * check below — the same message, by a longer route. They are kept as the
 * reference states them, so a reader is not left thinking the segment
 * alphabet is the only thing standing between a typo and a live route.
 */
function templateParameters(template: string): readonly string[] {
  if (!template.startsWith("/api/") || template.endsWith("/") || template.includes("?") || template.includes("#")) {
    throw new RouteDefinitionError("invalid route template");
  }

  // The leading empty segment is dropped; `api` itself is then re-validated
  // as an ordinary static segment, which it always passes given the prefix
  // check above.
  const parameters: string[] = [];
  for (const segment of template.split("/").slice(1)) {
    if (segment === "") {
      throw new RouteDefinitionError("invalid route template");
    }
    if (segment.startsWith("{") && segment.endsWith("}")) {
      const name = segment.slice(1, -1);
      // Two groups of one name is a regex error in some engines and a silent
      // last-wins in others.
      if (!PARAMETER_NAMES.has(name) || parameters.includes(name)) {
        throw new RouteDefinitionError("invalid route template parameter");
      }
      parameters.push(name);
      // The brace tests are stated for the reader; neither brace is in the
      // static alphabet, so the segment test alone would refuse these too.
    } else if (segment.includes("{") || segment.includes("}") || !STATIC_SEGMENT.test(segment)) {
      throw new RouteDefinitionError("invalid route template");
    }
  }
  return parameters;
}

/**
 * Whether two validated templates can match the same path.
 *
 * Narrower than it looks: a static segment that no parameter value could
 * equal does not intersect a parameter, because the parameter alphabet is
 * stricter than a static segment's. Refusing those would forbid a legitimate
 * pair like `/api/things/{session_id}` and `/api/things/all.json`.
 */
function templatesIntersect(first: string, second: string): boolean {
  const firstSegments = first.split("/").slice(1);
  const secondSegments = second.split("/").slice(1);
  if (firstSegments.length !== secondSegments.length) {
    return false;
  }
  for (const [index, firstSegment] of firstSegments.entries()) {
    const secondSegment = secondSegments[index] as string;
    const firstIsParameter = firstSegment.startsWith("{");
    const secondIsParameter = secondSegment.startsWith("{");
    if (firstIsParameter && secondIsParameter) {
      continue;
    }
    if (firstIsParameter) {
      if (!WHOLE_PARAMETER.test(secondSegment)) {
        return false;
      }
      continue;
    }
    if (secondIsParameter) {
      if (!WHOLE_PARAMETER.test(firstSegment)) {
        return false;
      }
      continue;
    }
    if (firstSegment !== secondSegment) {
      return false;
    }
  }
  return true;
}

/**
 * Compile a validated template into a whole-path matcher.
 *
 * Anchored and single-line, so a path carrying a newline cannot end early and
 * claim a route: in multiline mode `$` would match before the newline and
 * `/api/sessions/w1\nx` would dispatch as `/api/sessions/w1`.
 */
function compileTemplate(template: string): RegExp {
  const parts = template
    .split("/")
    .slice(1)
    .map((segment) =>
      segment.startsWith("{") ? `(?<${segment.slice(1, -1)}>${PARAMETER_PATTERN})` : pyReEscape(segment),
    );
  return new RegExp(`^/${parts.join("/")}$`);
}

/** A validated, immutable collection of route definitions. */
export class RouteRegistry {
  readonly routes: readonly RouteDef[];
  readonly #compiled: readonly CompiledRoute[];

  constructor(routes: readonly RouteDef[]) {
    const signatures = new Set<string>();
    const compiled: CompiledRoute[] = [];
    for (const route of routes) {
      RouteRegistry.#validateRoute(route);
      const signature = `${route.method} ${route.template}`;
      // Checked before the intersection below, so an identical pair gets the
      // more precise complaint of the two.
      if (signatures.has(signature)) {
        throw new RouteDefinitionError(`duplicate route: ${signature}`);
      }
      for (const existing of compiled) {
        // Two templates that could match the same path would make dispatch
        // depend on declaration order, so one would silently shadow the other
        // one day.
        if (route.method === existing.route.method && templatesIntersect(route.template, existing.route.template)) {
          throw new RouteDefinitionError(
            `intersecting route: ${route.method} ${route.template} and ${existing.route.template}`,
          );
        }
      }
      signatures.add(signature);
      compiled.push({ route, pattern: compileTemplate(route.template) });
    }
    this.routes = [...routes];
    this.#compiled = compiled;
  }

  /** Validate one route's metadata, template and scope. */
  static #validateRoute(route: RouteDef): void {
    if (!HTTP_METHODS.includes(route.method)) {
      throw new RouteDefinitionError("route method must be an HttpMethod");
    }
    if (!ROUTE_SCOPES.includes(route.scope)) {
      throw new RouteDefinitionError("route scope must be a RouteScope");
    }
    // Both are keys a backend looks a handler up by, so a stray space would
    // make one unreachable.
    requireNormalized(route.operation, "operation");
    requireNormalized(route.capability, "capability");
    const parameters = templateParameters(route.template);
    if (route.scope === "session" && !parameters.includes("session_id")) {
      throw new RouteDefinitionError("session route template must include {session_id}");
    }
  }

  /**
   * The route and parameters for an exact method and path.
   *
   * An unknown verb is not an error — it is simply no route, which the caller
   * turns into a 404 or a 405 by asking what the path allows.
   */
  match(method: string, path: string): RouteMatch | undefined {
    // What arrives off the wire is not normalised for us. The path is not
    // folded, though: `/API/sessions` is a different path, not a shouted one.
    const normalized = method.toUpperCase() as HttpMethod;
    // Standing in for the reference's `HttpMethod(...)` conversion, which
    // raises on an unknown verb. Redundant on its own — an unknown verb
    // equals no route's method either way — and kept because the alternative
    // reads as though any string were a method.
    if (!HTTP_METHODS.includes(normalized)) {
      return undefined;
    }
    for (const { route, pattern } of this.#compiled) {
      if (route.method !== normalized) {
        continue;
      }
      const found = pattern.exec(path);
      if (found !== null) {
        return { route, params: { ...found.groups } };
      }
    }
    return undefined;
  }

  /**
   * The methods whose templates match this path, sorted.
   *
   * The value goes into an `Allow` header, so it has to be the same on every
   * request rather than whatever a set happened to iterate to.
   */
  allowedMethods(path: string): readonly HttpMethod[] {
    const methods = new Set<HttpMethod>();
    for (const { route, pattern } of this.#compiled) {
      if (pattern.test(path)) {
        methods.add(route.method);
      }
    }
    return [...methods].sort();
  }

  /** Throw when this table requires a capability the backend lacks. */
  validateCapabilities(capabilities: Iterable<string>): void {
    const available = new Set(capabilities);
    const missing = [...new Set(this.routes.map((route) => route.capability))].filter(
      (capability) => !available.has(capability),
    );
    if (missing.length > 0) {
      // Sorted, because the message is read by a person wiring a backend up.
      throw new RouteDefinitionError(`missing route capabilities: ${missing.sort().join(", ")}`);
    }
  }
}
