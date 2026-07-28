//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Binding the shared API contract to a backend's handlers.
 *
 * Port of the Python module `provide.uterm.server.routes.route_defs`, minus
 * the framework. The reference registers with FastAPI; there is no equivalent
 * to register with here, and none is needed — the shared table already
 * matches a method and a path to an operation, so this turns that into a
 * dispatcher that Node's `http` or a Worker's `fetch` can drive.
 */

import { API_ROUTE_REGISTRY, API_ROUTES, type HttpMethod, type RouteDef } from "../api-routes/index.ts";

/** A binding that cannot be made. Stands in for the reference's `ValueError`. */
export class RouteBindingError extends Error {}

/** What a handler is told about the request it is answering. */
export interface RouteContext {
  method: string;
  path: string;
  /** The path parameters the template pulled out. */
  params: Readonly<Record<string, string>>;
  route: RouteDef;
}

/** What a backend supplies for one capability. */
export type RouteHandler = (context: RouteContext) => Promise<Response>;

/** Decides whether a principal holds one of a route's required roles. */
export type RoleAuthorizer = (context: RouteContext, roles: readonly string[]) => boolean | Promise<boolean>;

/** How to bind. */
export interface BindOptions {
  roleAuthorizer?: RoleAuthorizer;
}

/** A dispatcher over the routes a backend bound. */
export interface BoundRouter {
  /** The routes this router answers for. */
  readonly routes: readonly RouteDef[];
  dispatch(method: string, path: string): Promise<Response>;
}

/**
 * The per-route guard, as the reference states it.
 *
 * In the reference this runs as a framework dependency, *after* the framework
 * has already matched the template — so "this path is not my route" there
 * always means the parameters were wrong, and is a 422. A dispatcher that
 * does its own matching reaches a different conclusion for a path no route
 * claims at all, which is why {@link bindApiRoutes} answers 404 for those
 * before ever calling this.
 *
 * @returns A refusal, or nothing when the request may proceed.
 */
export async function routeGuard(
  route: RouteDef,
  path: string,
  authorize?: (roles: readonly string[]) => boolean | Promise<boolean>,
): Promise<Response | undefined> {
  const match = API_ROUTE_REGISTRY.match(route.method, path);
  if (match === undefined || match.route !== route) {
    return refusal(422, "invalid route path parameters");
  }
  // Only routes that declare roles are role-checked: running the authorizer
  // on the rest would let a role check leak onto operations that never asked
  // for one.
  if (route.roles.length > 0 && authorize !== undefined) {
    // Deciding a role usually means reading a token, which is not
    // synchronous.
    if (!(await authorize(route.roles))) {
      return refusal(403, "insufficient role privileges");
    }
  }
  return undefined;
}

/** One refusal, in the shape the reference's framework emits. */
function refusal(status: number, detail: string, headers: Record<string, string> = {}): Response {
  return Response.json({ detail }, { status, headers });
}

/**
 * Bind selected shared routes to a backend's capability handlers.
 *
 * The handler map is checked against the *complete* shared inventory, not
 * merely the routes selected: a backend that can serve only part of the
 * contract is refused outright rather than registering the part it has. Half
 * a router is worse than none, because what did bind would answer and what
 * did not would 404 as though it had never existed.
 *
 * @throws {RouteBindingError} When a route is not in the shared table, a
 *   capability has no handler, or a route with required roles has no
 *   authorizer to guard it.
 */
export function bindApiRoutes(
  handlers: ReadonlyMap<string, RouteHandler>,
  routes: readonly RouteDef[],
  options: BindOptions = {},
): BoundRouter {
  const selected = [...routes];
  const shared = new Set(API_ROUTES);
  if (selected.some((route) => !shared.has(route))) {
    throw new RouteBindingError("route definition is not in API_ROUTES");
  }

  try {
    API_ROUTE_REGISTRY.validateCapabilities(handlers.keys());
  } catch (error) {
    throw new RouteBindingError((error as Error).message);
  }

  // Selecting a guarded route without the check that guards it would publish
  // it unguarded, which is the one mistake this layer exists to prevent.
  if (selected.some((route) => route.roles.length > 0) && options.roleAuthorizer === undefined) {
    throw new RouteBindingError("role_authorizer is required for routes with required roles");
  }

  /** The methods this binding accepts for one path, sorted for the header. */
  const allowedFor = (path: string): HttpMethod[] =>
    [
      ...new Set(
        selected
          .filter((route) => API_ROUTE_REGISTRY.match(route.method, path)?.route === route)
          .map((route) => route.method),
      ),
    ].sort();

  return {
    routes: selected,
    async dispatch(method: string, path: string): Promise<Response> {
      const match = API_ROUTE_REGISTRY.match(method, path);
      const route = match === undefined ? undefined : selected.find((entry) => entry === match.route);

      if (route === undefined || match === undefined) {
        // Only the methods this backend bound, not the whole table's — a
        // router that advertised operations it cannot serve would send a
        // client to a verb that 404s.
        const allowed = allowedFor(path);
        if (allowed.length > 0) {
          return refusal(405, "Method Not Allowed", { Allow: allowed.join(", ") });
        }
        // The path might still belong to a route whose grammar it fails, in
        // which case it is a bad request rather than an unknown one.
        const known = selected.some((entry) => matchesShape(path, entry.template));
        return known ? refusal(422, "invalid route path parameters") : refusal(404, "Not Found");
      }

      const context: RouteContext = { method, path, params: match.params, route };
      if (route.roles.length > 0 && options.roleAuthorizer !== undefined) {
        // Deciding a role usually means reading a token, which is not
        // synchronous.
        const authorized = await options.roleAuthorizer(context, route.roles);
        if (!authorized) {
          return refusal(403, "insufficient role privileges");
        }
      }
      // Looked up by capability, not by operation. Every route in the shared
      // table happens to name both the same, so nothing observable rests on
      // it today — but a capability is what a backend implements and an
      // operation is what a client calls, and two operations may one day
      // share one capability.
      //
      // Present because the capability check above passed for the whole
      // inventory.
      return (handlers.get(route.capability) as RouteHandler)(context);
    },
  };
}

/**
 * Whether a path has a template's shape, ignoring the parameter grammar.
 *
 * The route exists and the parameters are wrong, which is a different answer
 * from the operation not existing: one sends a client to fix its request, the
 * other sends it looking for a different endpoint.
 */
function matchesShape(path: string, template: string): boolean {
  const pathSegments = path.split("/").slice(1);
  const templateSegments = template.split("/").slice(1);
  if (pathSegments.length !== templateSegments.length) {
    return false;
  }
  return templateSegments.every((expected, index) => {
    const actual = pathSegments[index] as string;
    return expected.startsWith("{") ? actual !== "" : actual === expected;
  });
}
