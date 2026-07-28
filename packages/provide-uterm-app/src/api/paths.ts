//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Request paths built from the shared API contract.
 *
 * The server and the Worker both dispatch from `API_ROUTES`, so the SPA
 * builds its calls from the same table rather than writing the paths out
 * again. A route that moves, or a method that changes, reaches this side as a
 * failing test instead of a 404 in a browser.
 */

import { API_ROUTE_REGISTRY, API_ROUTES, type HttpMethod } from "provide-uterm-ts/api-routes";

/** An operation the shared contract does not have. */
export class UnknownOperationError extends Error {}

/** A parameter the template does not name, is missing, or could not be matched. */
export class UnusableParameterError extends Error {}

/** What to send, and where. */
export interface RouteCall {
  method: HttpMethod;
  path: string;
}

/** The parameter names a template holds, in the order they appear. */
function templateParameters(template: string): readonly string[] {
  return template
    .split("/")
    .filter((segment) => segment.startsWith("{"))
    .map((segment) => segment.slice(1, -1));
}

/**
 * The method and path for one operation.
 *
 * The built path is matched back against the table before it is returned. A
 * value carrying a slash would otherwise address a different route entirely,
 * and one carrying a dot or a percent escape would address none — failing at
 * the server, which can only report that something was wrong, rather than
 * here, where the bad value is still in hand.
 */
export function routeCall(operation: string, params: Readonly<Record<string, string>> = {}): RouteCall {
  const route = API_ROUTES.find((entry) => entry.operation === operation);
  if (route === undefined) {
    throw new UnknownOperationError(`unknown operation: ${operation}`);
  }

  const expected = templateParameters(route.template);
  for (const name of Object.keys(params)) {
    // Ignoring it would leave the caller believing it had been sent.
    if (!expected.includes(name)) {
      throw new UnusableParameterError(`${operation} takes no parameter ${name}`);
    }
  }

  let path = route.template;
  for (const name of expected) {
    const value = params[name];
    // The alternative is a path with a literal `{session_id}` in it.
    if (value === undefined) {
      throw new UnusableParameterError(`${operation} needs a ${name}`);
    }
    path = path.replace(`{${name}}`, value);
  }

  const found = API_ROUTE_REGISTRY.match(route.method, path);
  if (found?.route.operation !== operation) {
    throw new UnusableParameterError(
      `${operation} cannot be addressed with ${expected.map((name) => `${name}=${params[name]}`).join(", ")}`,
    );
  }
  return { method: route.method, path };
}
