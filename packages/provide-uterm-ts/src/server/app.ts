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

import { API_ROUTES, type RouteDef } from "../api-routes/index.ts";
import type { InputMode } from "../hub/index.ts";
import {
  ANONYMOUS_SUBJECT,
  type AuthSettings,
  resolveJwtPrincipal,
  type ServerPrincipal,
} from "../serverauth/index.ts";
import { getLogger, type Logger } from "../telemetry/index.ts";
import { type AuthorizableSession, canMutateSession, canReadSession, isAdmin } from "./authorization.ts";
import { healthReport, livenessReport, readinessReport } from "./health.ts";
import { handleHijackRequest, INPUT_MODES, readJsonBody as readBody } from "./hijack-routes.ts";
import { type RecordingAccess, recordingHandlers } from "./recording-routes.ts";
import { bindApiRoutes, matchesShape, type RouteHandler } from "./route-binding.ts";
import type { SessionHub } from "./session-hub.ts";
import { filterSessions, type SessionListQuery, type SessionRegistry } from "./session-registry.ts";
import {
  isShareSubjectOf,
  SHARE_VIEWER_REVOKE_BODY_MAX_BYTES,
  ShareViewerRevocations,
  ShareViewerSubjectRevocationsFullError,
  VIEWER_REVOKED_CLOSE_CODE,
  VIEWER_REVOKED_REASON,
} from "./viewer-revocation.ts";

/** The refusal every unauthenticated request gets, whatever it asked for. */
export const UNAUTHENTICATED_DETAIL = "authentication required";

/** What a mode nobody defined is refused with. It names both permitted values. */
export const INPUT_MODE_DETAIL = "input_mode must be 'open' or 'hijack'";

/** The capabilities this server implements, as the shared table names them. */
export const SERVED_CAPABILITIES: readonly string[] = [
  "sessions.list",
  "sessions.get",
  "sessions.snapshot",
  "sessions.set_mode",
  "sessions.recording",
  "sessions.recording_entries",
  "sessions.recording_download",
];

/** How a session's connector is reached, when one is running. */
export interface ConnectorAccess {
  /** Tell a running connector its input mode changed. */
  setMode(sessionId: string, mode: InputMode): Promise<void>;
}

/** What a server app is built with. */
export interface ServerAppOptions {
  registry: SessionRegistry;
  auth: AuthSettings;
  /** The hub the lease routes arbitrate through. */
  hub: SessionHub;
  /** The running connectors, for the routes that change one. */
  connectors: ConnectorAccess;
  /** Where recordings are read back from: bootstrap supplies the runtimes. */
  recordings: RecordingAccess;
  /** The version health reports. */
  version: string;
  /** Which store is behind the control plane, as health reports it. */
  controlPlaneBackend: string;
  /** When the process started, in seconds. */
  startupTime: number;
  /** The clock, in seconds. The runtime's own unless a test says otherwise. */
  now?: (() => number) | undefined;
  /** Where audit records go. The runtime's own logger unless one is supplied. */
  logger?: Logger | undefined;
}

/** A built application. */
export interface ServerApp {
  /**
   * Answer one request.
   *
   * `clientAddress` is the connection's own remote address, which the lease
   * routes charge their rate limit against. It is a second argument and not a
   * header because a header is a thing the client writes: the reference reads
   * the socket for exactly this reason, and a runtime that cannot say where a
   * request came from passes nothing and shares one bucket with the others.
   */
  handle(request: Request, clientAddress?: string): Promise<Response>;
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

/**
 * The viewer-revocation route, `POST /api/sessions/{session_id}/viewers/revoke`.
 *
 * Runtime-native like the lease routes, not part of the shared contract: the
 * Worker has no share-viewer grant to revoke.
 */
const VIEWERS_REVOKE_PATH = /^\/api\/sessions\/([^/]+)\/viewers\/revoke$/;

/** A session id the reference's route grammar accepts. */
const SESSION_ID = /^[\w-]+$/;

/** One refusal, in the shape the reference's framework emits. */
function refusal(status: number, detail: string, headers: Record<string, string> = {}): Response {
  return Response.json({ detail }, { status, headers });
}

/** A surrogate that is not half of a pair. */
const LONE_SURROGATE = /\p{Cs}/u;

/** JSON's own whitespace (RFC 8259), the only blank a revoke body may be. */
const JSON_WHITESPACE = /^[ \t\r\n]+|[ \t\r\n]+$/g;

/**
 * The share subject a revoke body names, `undefined` to revoke every share
 * viewer, or the refusal to answer with.
 *
 * No body, a blank one, `null` and `{}` all mean the whole session, as before
 * the body existed (`null` is "no body" across the ports, as in the reference's
 * optional bodies). Anything else must be exactly `{"subject": "<string>"}`:
 * an unknown key (a misspelt `subject`) or a `null` subject is refused rather
 * than read as "every viewer", which would close sockets the caller meant to
 * leave open. Invalid UTF-8 and a byte-order mark are refused, as in the other
 * ports.
 */
async function requestedSubject(request: Request, sessionId: string): Promise<string | undefined | Response> {
  const invalid = (): Response => refusal(422, "invalid request body");
  const raw = await cappedBody(request);
  if (raw === undefined) {
    return invalid();
  }
  let body: unknown;
  try {
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw).replace(JSON_WHITESPACE, "");
    if (text === "") {
      return undefined;
    }
    body = JSON.parse(text);
  } catch {
    return invalid();
  }
  if (body === null) {
    return undefined;
  }
  if (typeof body !== "object" || Array.isArray(body)) {
    return invalid();
  }
  const keys = Object.keys(body);
  if (keys.some((key) => key !== "subject")) {
    return invalid();
  }
  if (keys.length === 0) {
    return undefined;
  }
  const subject: unknown = (body as { subject: unknown }).subject;
  // JSON can spell a lone surrogate, which is not text: in a `u` pattern a
  // surrogate code point only matches when it is not half of a pair.
  if (typeof subject !== "string" || LONE_SURROGATE.test(subject)) {
    return invalid();
  }
  return isShareSubjectOf(subject, sessionId)
    ? subject
    : refusal(422, "subject must be a share subject of this session");
}

/**
 * A request's body, or `undefined` once it runs past
 * {@link SHARE_VIEWER_REVOKE_BODY_MAX_BYTES}: reading stops at the chunk that
 * crosses the cap and the rest is never read, since the largest body worth
 * reading is a 512-byte subject in its envelope.
 */
async function cappedBody(request: Request): Promise<Uint8Array | undefined> {
  if (request.body === null) {
    return new Uint8Array();
  }
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) {
    size += chunk.value.length;
    if (size > SHARE_VIEWER_REVOKE_BODY_MAX_BYTES) {
      await reader.cancel();
      return undefined;
    }
    chunks.push(chunk.value);
  }
  const whole = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    whole.set(chunk, offset);
    offset += chunk.length;
  }
  return whole;
}

/** A revoke's answer: the cutoff, the close a revoked socket would get, and any subject applied. */
function revokedResponse(sessionId: string, revokedAt: number, subject?: string): Response {
  return Response.json({
    session_id: sessionId,
    revoked_at: revokedAt,
    closed: 0,
    close_code: VIEWER_REVOKED_CLOSE_CODE,
    close_reason: VIEWER_REVOKED_REASON,
    // The subject that was applied, so a caller can see its body was honoured.
    // Absent for a whole-session revoke, whose answer keeps its original shape.
    ...(subject === undefined ? {} : { subject }),
  });
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
 * Annotate a snapshot with how old it is and where it came from.
 *
 * Ports `SessionRegistry._stamp_snapshot_freshness` from the Python reference.
 * A snapshot carries a `ts` and nothing else about its provenance, so a client
 * polling this endpoint sees a screen with no way to tell "the terminal is
 * idle" from "the worker stopped answering an hour ago" — both render as the
 * same unchanging screen.
 *
 * `snapshot_source` is always `"cache"` here, and `snapshot_requested` always
 * `false`: this hub has no `waitForSnapshot`, so there is no way to ask the
 * worker for a fresh screen. That is exactly what the reference reports for a
 * read with no `wait_ms`, so the answers agree — but a caller cannot force a
 * round trip against this implementation the way it can against the reference.
 * Porting that needs a polling method on the hub, which is a larger change than
 * this one and is deliberately not smuggled in here.
 */
export function stampSnapshotFreshness(snapshot: Record<string, unknown>): Record<string, unknown> {
  const ts = snapshot.ts;
  // `> 0` rejects the absent-timestamp default of 0, which is not 1970 but "not
  // set" — dating it would report every such snapshot as decades stale.
  // Number.isFinite also rejects NaN/Infinity, which JSON can carry in.
  const usable = typeof ts === "number" && Number.isFinite(ts) && ts > 0;
  snapshot.snapshot_age_ms = usable
    ? // Clamped at zero: clock skew between the worker and this process must
      // not produce a negative age, which says nothing and would sail past any
      // staleness threshold a consumer compares against.
      Math.max(0, Math.round(Date.now() - (ts as number) * 1000))
    : null;
  snapshot.snapshot_source = "cache";
  snapshot.snapshot_requested = false;
  return snapshot;
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
  const recordings = options.recordings;
  const revocations = new ShareViewerRevocations();
  const logger = options.logger ?? getLogger("provide.uterm.server.viewers");

  /** The read decision, refusing a share viewer whose access was revoked. */
  function canRead(principal: ServerPrincipal, session: AuthorizableSession): boolean {
    return canReadSession(principal, session) && !revocations.revoked(principal, session.session_id);
  }

  /**
   * The handler map for one request.
   *
   * Rebuilt per request because a handler needs the principal that was
   * resolved for it and the query it was called with, and the binding is a
   * table of capability to function with nowhere to put either. Building it
   * is a filter over forty routes, which is not the cost of anything.
   */
  function handlers(request: Request, principal: ServerPrincipal, url: URL): ReadonlyMap<string, RouteHandler> {
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
      const visible = registry.statuses().filter((status) => canRead(principal, status));
      return Response.json(filterSessions(visible, query));
    });

    map.set("sessions.get", async (context) => {
      const sessionId = context.params.session_id as string;
      const definition = registry.definition(sessionId);
      if (definition === undefined) {
        return refusal(404, `unknown session: ${sessionId}`);
      }
      if (!canRead(principal, definition)) {
        return refusal(403, "insufficient privileges");
      }
      // Present, because its definition is.
      return Response.json(registry.status(sessionId) as object);
    });

    map.set("sessions.snapshot", async (context) => {
      const sessionId = context.params.session_id as string;
      const definition = registry.definition(sessionId);
      if (definition === undefined) {
        return refusal(404, `unknown session: ${sessionId}`);
      }
      if (!canRead(principal, definition)) {
        return refusal(403, "insufficient privileges");
      }
      // A session whose worker has never sent a screen answers `null` with a
      // 200, not a 404: the session exists and has nothing to show yet, which
      // is a different thing from a session that does not exist.
      const snapshot = await options.hub.getLastSnapshot(sessionId);
      return Response.json(snapshot === undefined ? null : stampSnapshotFreshness(snapshot));
    });

    map.set("sessions.set_mode", async (context) => {
      const sessionId = context.params.session_id as string;
      const definition = registry.definition(sessionId);
      if (definition === undefined) {
        return refusal(404, `unknown session: ${sessionId}`);
      }
      // Existence, then privilege, then the payload — a caller who may not
      // touch the session learns nothing about whether their body was valid.
      if (!canMutateSession(principal, definition, "session.control.mode")) {
        return refusal(403, "insufficient privileges");
      }
      const body = await readBody(request);
      const mode = String(body.input_mode ?? "").trim();
      if (!INPUT_MODES.has(mode)) {
        return refusal(422, INPUT_MODE_DETAIL);
      }
      await setSessionMode(sessionId, mode as InputMode);
      // Present, because its definition is.
      return Response.json(registry.status(sessionId) as object);
    });
    for (const [capability, handler] of recordingHandlers({
      registry,
      recordings,
      principal,
      request,
      url,
      revocations,
    })) {
      map.set(capability, handler);
    }
    return map;
  }

  /**
   * Move one session between `open` and `hijack`.
   *
   * Three places hold the mode and all three are written, in the reference's
   * order. The hub's copy is what an acquire is refused against, so a port
   * that changed only the definition would report `hijack` to every reader
   * while still refusing every lease with "not available in open input mode".
   *
   * Opening a session releases whatever is held on it first. In open mode the
   * lease stops gating input, so leaving one in place would put a holder in
   * the position of believing they alone are driving a terminal that everyone
   * can now type into.
   */
  async function setSessionMode(sessionId: string, mode: InputMode): Promise<void> {
    if (mode === "open") {
      await options.hub.forceReleaseHijack(sessionId);
    }
    options.registry.setInputMode(sessionId, mode);
    await options.connectors.setMode(sessionId, mode);
    await options.hub.setInputMode(sessionId, mode);
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

  /**
   * Revoke a session's share viewers, or one share subject of it when the
   * body names one: record the cutoff and report it.
   *
   * The same gate as everything else here — method, credential, grammar,
   * existence, privilege — and the privilege is `session.control.update`, so
   * the owner, a session-scoped admin or a global admin. The body is judged
   * after the gate, so a refused caller's body is never read. No socket can
   * be open to close, so `closed` is always zero.
   */
  async function revokeViewers(
    request: Request,
    method: string,
    sessionId: string,
    principal: ServerPrincipal,
    authenticated: boolean,
  ): Promise<Response> {
    if (method !== "POST") {
      return refusal(405, "Method Not Allowed", { Allow: "POST" });
    }
    if (!authenticated) {
      return unauthenticated();
    }
    if (!SESSION_ID.test(sessionId)) {
      return refusal(422, "invalid route path parameters");
    }
    const definition = options.registry.definition(sessionId);
    if (definition === undefined) {
      return refusal(404, `unknown session: ${sessionId}`);
    }
    if (!canMutateSession(principal, definition, "session.control.update")) {
      return refusal(403, "insufficient privileges");
    }
    const subject = await requestedSubject(request, sessionId);
    if (subject instanceof Response) {
      return subject;
    }
    const now = (options.now ?? (() => Date.now() / 1000))();
    let revokedAt: number;
    if (subject !== undefined) {
      try {
        revokedAt = revocations.revokeSubject(sessionId, subject, now);
      } catch (error) {
        // The two things revokeSubject throws: this session's cap, or the
        // whole subject table's (ShareViewerRevocationsFullError).
        return error instanceof ShareViewerSubjectRevocationsFullError
          ? refusal(429, "too many viewers of this session revoked one by one; revoke them all instead")
          : refusal(429, "too many viewers revoked one by one; try again later");
      }
      return revokedResponse(sessionId, revokedAt, subject);
    }
    try {
      // The embedder's service token is a global admin and revokes for
      // everyone, so only other callers are held to a per-principal share.
      const revoker = isAdmin(principal) ? undefined : principal.subject_id;
      revokedAt = revocations.revoke(sessionId, now, revoker, (evicted) => {
        // The cap made room by dropping this principal's oldest cutoff, so
        // that session's earlier share tokens are admitted again: say so.
        logger.info(
          {
            action: "session.viewers.cutoff_evicted",
            principal: principal.subject_id,
            session_id: evicted,
            revoked_session_id: sessionId,
          },
          "audit_event",
        );
      });
    } catch {
      // The one thing revoke throws: ShareViewerRevocationsFullError.
      return refusal(429, "too many sessions with revoked viewers; try again later");
    }
    return revokedResponse(sessionId, revokedAt);
  }

  async function handle(request: Request, clientAddress?: string): Promise<Response> {
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

    const principal = principalOf(request);
    const authenticated = principal.subject_id !== ANONYMOUS_SUBJECT;

    // The lease routes are not part of the shared contract — they are the
    // hub's own — so they are matched here rather than through the table.
    // Ahead of it, because nothing in the table claims a `/worker/` path.
    const lease = await handleHijackRequest(request, {
      hub: options.hub,
      registry: options.registry,
      principal,
      authenticated,
      clientAddress,
      revocations,
    });
    if (lease !== undefined) {
      return lease;
    }

    const revoke = VIEWERS_REVOKE_PATH.exec(path);
    if (revoke !== null) {
      return revokeViewers(request, method, revoke[1] as string, principal, authenticated);
    }

    // A request for a served route is one whose caller has to have
    // authenticated. Matched by method and shape, so that a path nobody routes
    // stays a 404 for everyone and a wrong verb a 405, as in the reference;
    // that existence is never revealed to a caller who has not identified
    // themselves; and that a path whose id fails the route's grammar is still
    // that route's — the reference refuses an anonymous caller before it
    // validates anything, so the grammar of an id is not something an
    // anonymous caller can probe.
    const served = SERVED_ROUTES.some((route) => route.method === method && matchesShape(path, route.template));
    if (served && !authenticated) {
      return unauthenticated();
    }
    return bindApiRoutes(handlers(request, principal, url), SERVED_ROUTES).dispatch(method, path);
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
