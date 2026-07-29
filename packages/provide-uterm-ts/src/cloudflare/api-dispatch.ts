//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a request to `/api/...` is answered with before any handler sees it.
 *
 * Port of `provide.uterm.cloudflare.entry.route_defs`. The four answers are
 * deliberately different from one another.
 *
 * **405, not 404**, when the path is a route but the method is not one it
 * takes — with an `Allow` header naming what it does take, because a caller
 * that guessed the verb deserves to be told rather than left to conclude the
 * route does not exist.
 *
 * **422, not 404**, when the path has a route's *shape* but a parameter the
 * registry refuses — an id with a dot or a space in it. That is the difference
 * between "you spelled the id wrong" and "there is no such endpoint", and
 * collapsing the two would hide a caller's real mistake.
 *
 * **404** only when nothing matches at all.
 *
 * Then, in order: authenticate, check the route's role alternatives, run the
 * capability. A route naming no roles skips the second check — but every route
 * is authenticated first, so an empty list means *any authenticated caller*
 * and never *anyone*.
 */

import { API_ROUTES, type HttpMethod, type RouteDef, type RouteScope } from "../api-routes/index.ts";

/** What the dispatcher decided, before any handler ran. */
export type DispatchOutcome =
  | { outcome: "not_api" }
  | { outcome: "method_not_allowed"; status: 405; allow: readonly HttpMethod[] }
  | { outcome: "invalid_route_parameter"; status: 422 }
  | { outcome: "not_found"; status: 404 }
  | {
      outcome: "matched";
      capability: string;
      scope: RouteScope;
      roles: string[];
      params: Record<string, string>;
    };

/** What the caller's registry has to be able to answer. */
export interface DispatchRegistry {
  match(method: string, path: string): { route: RouteDef; params: Record<string, string> } | undefined;
  allowedMethods(path: string): readonly HttpMethod[];
}

/** Only API paths reach this dispatcher at all. */
export const API_PREFIX = "/api/";

/**
 * Whether a path has a route's segments, ignoring what the parameters say.
 *
 * This is the whole of the 422-versus-404 decision: a path with the right
 * shape and a refused parameter is a caller's typo, and one with the wrong
 * shape is a route that does not exist.
 */
export function matchesRouteShape(path: string, template: string): boolean {
  const pathSegments = path.split("/").slice(1);
  const templateSegments = template.split("/").slice(1);
  if (pathSegments.length !== templateSegments.length) {
    return false;
  }
  return pathSegments.every((actual, index) => {
    const expected = templateSegments[index] as string;
    // A parameter matches anything that is *there*; an empty segment is not.
    return expected.startsWith("{") ? actual !== "" : actual === expected;
  });
}

/**
 * What to answer a request with, before any handler runs.
 *
 * @param registry The shared route registry, which both backends dispatch
 *   from — so a route added on one side cannot be missing from the other.
 */
export function dispatchApiRoute(method: string, path: string, registry: DispatchRegistry): DispatchOutcome {
  if (!path.startsWith(API_PREFIX)) {
    return { outcome: "not_api" };
  }
  // Not upper-cased here: the registry folds the verb itself, and doing it
  // twice would read as though one of the two were load-bearing.
  const match = registry.match(method, path);
  if (match !== undefined) {
    return {
      outcome: "matched",
      capability: match.route.capability,
      scope: match.route.scope,
      roles: [...match.route.roles].sort(),
      params: match.params,
    };
  }

  const allow = registry.allowedMethods(path);
  if (allow.length > 0) {
    return { outcome: "method_not_allowed", status: 405, allow };
  }
  if (API_ROUTES.some((route) => matchesRouteShape(path, route.template))) {
    return { outcome: "invalid_route_parameter", status: 422 };
  }
  return { outcome: "not_found", status: 404 };
}

/**
 * Whether a caller's roles satisfy a route.
 *
 * A route naming no roles is satisfied by anybody who got this far — which is
 * anybody authenticated, because authentication runs first. A route naming
 * some is satisfied by holding *any* one of them: they are alternatives, not
 * requirements.
 */
export function rolesSatisfyRoute(route: Pick<RouteDef, "roles">, roles: readonly string[]): boolean {
  if (route.roles.length === 0) {
    return true;
  }
  return route.roles.some((required) => roles.includes(required));
}

/**
 * Check that every global route has a handler and no session route does.
 *
 * Run at import in the reference, and for a reason: a route added without a
 * handler should stop the Worker starting rather than 500 at request time.
 * A *session* route with a handler here would be worse — it would answer from
 * the Worker instead of the session's own Durable Object, which is where its
 * state lives.
 *
 * @throws {Error} On either mistake, naming which.
 */
export function validateGlobalCapabilities(
  handlers: Readonly<Record<string, unknown>>,
  routes: readonly RouteDef[] = API_ROUTES,
): void {
  const sessionCapabilities = routes.filter((route) => route.scope === "session").map((route) => route.capability);
  const registeredSession = sessionCapabilities.filter((capability) => capability in handlers);
  if (registeredSession.length > 0) {
    throw new Error("session RouteDef capability registered in Worker");
  }
  const missing = [
    ...new Set(
      routes
        .filter((route) => route.scope === "global")
        .map((route) => route.capability)
        .filter((capability) => !(capability in handlers)),
    ),
  ].sort();
  if (missing.length > 0) {
    throw new Error(`missing Worker route capabilities: ${missing.join(", ")}`);
  }
}
