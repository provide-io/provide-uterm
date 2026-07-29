//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The HTTP application: a request goes in, a response comes out.
 *
 * Port of what `provide.uterm.server.app.routes_wiring` assembles — the
 * health router, the shared session routes, and the `require_authenticated`
 * dependency that sits in front of everything but the probes.
 *
 * Framework-neutral on purpose. Node's `http` and a Worker's `fetch` both hand
 * over a method, a URL and some headers, and this needs nothing else; the
 * Node binding lives in `node-http.ts` and is the only part that imports a
 * runtime.
 *
 * Three orderings here are contract rather than implementation:
 *
 * * **Route first, then authenticate.** A path in no route table is a 404
 *   whether or not the caller has a token, which is what the reference's
 *   framework does — matching happens before a dependency runs.
 * * **Authenticate before existence.** A caller with no credential asking for
 *   a session that does not exist gets 401 and not 404. The other way round,
 *   anyone could enumerate session ids by reading the status code.
 * * **One refusal for two failures.** No credential and a credential that
 *   does not verify produce the same status and the same bytes. A different
 *   message for either is an oracle for whether a guess was well-formed.
 */

import { API_ROUTE_REGISTRY, API_ROUTES, type RouteDef } from "../api-routes/index.ts";
import {
  ANONYMOUS_SUBJECT,
  type AuthSettings,
  resolveJwtPrincipal,
  type ServerPrincipal,
} from "../serverauth/index.ts";
import { canReadSession } from "./authorization.ts";
import { healthReport, livenessReport, readinessReport } from "./health.ts";
import { bindApiRoutes, type RouteHandler } from "./route-binding.ts";
import { filterSessions, type SessionListQuery, type SessionRegistry } from "./session-registry.ts";

/** The refusal every unauthenticated request gets, whatever it asked for. */
export const UNAUTHENTICATED_DETAIL = "authentication required";

/** The capabilities this server implements, as the shared table names them. */
export const SERVED_CAPABILITIES: readonly string[] = ["sessions.list", "sessions.get"];

/** What a server app is built with. */
export interface ServerAppOptions {
  registry: SessionRegistry;
  auth: AuthSettings;
  /** The version health reports. */
  version: string;
  /** Which store is behind the control plane, as health reports it. */
  controlPlaneBackend: string;
  /** When the process started, in seconds. */
  startupTime: number;
  /** The clock, in seconds. The runtime's own unless a test says otherwise. */
  now?: (() => number) | undefined;
}

/** A built application. */
export interface ServerApp {
  /** Answer one request. */
  handle(request: Request): Promise<Response>;
  /**
   * Whether startup finished.
   *
   * Settable because it is: the probes answer differently before and after, and a
   * server that reported ready from the first instant would take traffic it
   * could not yet serve.
   */
  ready: boolean;
}

/** One endpoint that is this runtime's own rather than the shared contract's. */
interface OperationalRoute {
  method: string;
  path: string;
  answer: (app: BuiltApp) => { status: number; body: unknown };
}

/** The parts of the app an operational handler reads. */
interface BuiltApp {
  options: ServerAppOptions;
  ready: boolean;
}

/**
 * The probes, which carry no authentication.
 *
 * They are not in `api-routes` because they are not part of the shared
 * contract: the Worker has its own liveness story. They are still routed from
 * a table, so that a wrong method on one is a 405 rather than a 404 — the
 * difference between telling a client to fix its verb and sending it looking
 * for another endpoint.
 *
 * Every one of them is public, and there is no flag here saying so. The
 * reference has exactly one authenticated endpoint outside the shared
 * contract — `/api/security-posture`, which reports the deployment's own
 * relaxations and must never be anonymous — and this port does not serve it.
 * A flag for a case nothing takes would be a guard nothing runs.
 */
const OPERATIONAL: readonly OperationalRoute[] = [
  {
    method: "GET",
    path: "/api/health",
    answer: (app) =>
      healthReport({
        registryAttached: true,
        ready: app.ready,
        startupTime: app.options.startupTime,
        version: app.options.version,
        activeSessions: app.options.registry.size,
        controlPlaneBackend: app.options.controlPlaneBackend,
        now: (app.options.now ?? (() => Date.now() / 1000))(),
      }),
  },
  { method: "GET", path: "/healthz", answer: () => livenessReport() },
  { method: "GET", path: "/readyz", answer: (app) => readinessReport(app.ready) },
];

/** One refusal, in the shape the reference's framework emits. */
function refusal(status: number, detail: string, headers: Record<string, string> = {}): Response {
  return Response.json({ detail }, { status, headers });
}

/** The one answer both ways of failing to authenticate produce. */
function unauthenticated(): Response {
  return refusal(401, UNAUTHENTICATED_DETAIL);
}

/** An integer query parameter, or a refusal when it is outside its bounds. */
function boundedInteger(raw: string | null, fallback: number, low: number, high: number): number | undefined {
  if (raw === null) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value < low || value > high) {
    return undefined;
  }
  return value;
}

/**
 * Read the query a session list was asked for.
 *
 * `undefined` means the query itself is invalid, which is a 422 — the
 * reference reaches the same status through its framework's own validation.
 */
export function parseListQuery(parameters: URLSearchParams): SessionListQuery | undefined {
  const q = parameters.get("q");
  if (q !== null && q.length > 200) {
    return undefined;
  }
  const limit = boundedInteger(parameters.get("limit"), 50, 1, 200);
  const offset = boundedInteger(parameters.get("offset"), 0, 0, Number.MAX_SAFE_INTEGER);
  if (limit === undefined || offset === undefined) {
    return undefined;
  }
  return {
    tag: parameters.getAll("tag"),
    connector_type: parameters.get("connector_type") ?? undefined,
    visibility: parameters.get("visibility") ?? undefined,
    state: parameters.get("state") ?? undefined,
    q: q ?? undefined,
    sort: parameters.get("sort") ?? "created_at",
    order: parameters.get("order") ?? "desc",
    limit,
    offset,
  };
}

/**
 * The handler a capability nobody bound gets.
 *
 * The binding is checked against the *complete* shared inventory rather than
 * the routes selected, so every capability needs an entry even when its route
 * is never selected. The reference does the same, and for the same reason:
 * half a router is worse than none, because what did bind would answer and
 * what did not would 404 as though it had never existed.
 *
 * It raises rather than answering. Reaching it would mean a route was
 * selected whose handler was never written, which is a fault in the wiring
 * and not something a client should be told a status about.
 */
export async function unservedCapability(): Promise<Response> {
  throw new Error("unregistered shared API capability invoked");
}

/** The shared routes this server binds, which is the read half of the family. */
export const SERVED_ROUTES: readonly RouteDef[] = API_ROUTES.filter((route) =>
  SERVED_CAPABILITIES.includes(route.capability),
);

/** Build the application. */
export function createServerApp(options: ServerAppOptions): ServerApp {
  const app: BuiltApp = { options, ready: true };

  /**
   * The handler map for one request.
   *
   * Rebuilt per request because a handler needs the principal that was
   * resolved for it and the query it was called with, and the binding is a
   * table of capability to function with nowhere to put either. Building it
   * is a filter over forty routes, which is not the cost of anything.
   */
  function handlers(principal: ServerPrincipal, url: URL): ReadonlyMap<string, RouteHandler> {
    const registry = options.registry;
    const map = new Map<string, RouteHandler>();
    for (const route of API_ROUTES) {
      map.set(route.capability, unservedCapability);
    }

    map.set("sessions.list", async () => {
      const query = parseListQuery(url.searchParams);
      if (query === undefined) {
        return refusal(422, "invalid query parameters");
      }
      // Filtered by what this caller may read *before* anything is narrowed,
      // so a paged list never spends its page on sessions it then hides.
      const visible = registry.statuses().filter((status) => canReadSession(principal, status));
      return Response.json(filterSessions(visible, query));
    });

    map.set("sessions.get", async (context) => {
      const sessionId = context.params.session_id as string;
      const definition = registry.definition(sessionId);
      if (definition === undefined) {
        return refusal(404, `unknown session: ${sessionId}`);
      }
      if (!canReadSession(principal, definition)) {
        return refusal(403, "insufficient privileges");
      }
      // Present, because its definition is.
      return Response.json(registry.status(sessionId) as object);
    });
    return map;
  }

  /**
   * Who a request is.
   *
   * The app's own clock, not the runtime's: a server built with a pinned
   * clock must verify tokens against the same instant it mints them with, or
   * it would refuse its own credential.
   */
  function principalOf(request: Request): ServerPrincipal {
    return resolveJwtPrincipal(request.headers, options.auth, options.now);
  }

  async function handle(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    const onThisPath = OPERATIONAL.filter((route) => route.path === path);
    if (onThisPath.length > 0) {
      const route = onThisPath.find((one) => one.method === method);
      if (route === undefined) {
        const allowed = [...new Set(onThisPath.map((one) => one.method))].sort();
        return refusal(405, "Method Not Allowed", { Allow: allowed.join(", ") });
      }
      const answer = route.answer(app);
      return Response.json(answer.body, { status: answer.status });
    }

    // A route matched is a route whose caller has to have authenticated. The
    // match happens first so that a path nobody routes stays a 404 for
    // everyone, and second so that existence is never revealed to a caller
    // who has not identified themselves.
    const match = API_ROUTE_REGISTRY.match(method, path);
    const served = match !== undefined && SERVED_ROUTES.includes(match.route);
    const principal = principalOf(request);
    if (served && principal.subject_id === ANONYMOUS_SUBJECT) {
      return unauthenticated();
    }
    return bindApiRoutes(handlers(principal, url), SERVED_ROUTES).dispatch(method, path);
  }

  return {
    handle,
    get ready(): boolean {
      return app.ready;
    },
    set ready(value: boolean) {
      app.ready = value;
    },
  };
}
