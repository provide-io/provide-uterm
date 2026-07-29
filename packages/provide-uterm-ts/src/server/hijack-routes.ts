//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The lease routes: `/worker/{worker_id}/hijack/...`.
 *
 * Port of `provide.uterm.server.bridge.routes.rest` together with the gate in
 * front of it, `provide.uterm.server.app.hub_authz`. The two are separate
 * modules in the reference for a reason worth restating: the hub's router
 * carries *no* authentication of its own, and the gate is the only thing
 * standing between an unauthenticated caller and control of somebody's
 * terminal. Here they are one file so that neither can be mounted without the
 * other.
 *
 * ## Two envelopes, and which is which
 *
 * The gate refuses with `detail` — it is the framework's own refusal, the same
 * one every other route in the application produces. The lease routes refuse
 * with `error` — that is the hub router's shape. Both are the reference's, and
 * a port that picked one for everything would be wrong half the time.
 *
 * The split is not arbitrary once you see where each refusal comes from:
 *
 * | Refusal | Envelope | Whose |
 * |---|---|---|
 * | 401 not authenticated | `detail` | the gate |
 * | 404 unknown session | `detail` | the gate |
 * | 403 insufficient privileges | `detail` | the gate |
 * | 409 open mode / already hijacked | `error` | the lease |
 * | 404 invalid or expired lease | `error` | the lease |
 *
 * Note the wording of the gate's 404 as well as its shape: it calls the thing
 * a *session* even though the route is a worker route. One thing has two names
 * depending on which side of the hub you stand on, and the message uses the
 * name the caller configured it under.
 *
 * ## The budget, and where it sits
 *
 * Acquires, sends and steps are charged against the caller's address and
 * answered `429 {"error": "rate_limited"}` over the limit. Three things about
 * that are the reference's and are observable from outside:
 *
 * * **Behind the gate.** An unauthenticated flood gets 401, not 429, and a
 *   caller who may not touch the session gets 403. Nobody learns the budget
 *   without a credential that works.
 * * **In front of the lease.** A send over the budget is 429 even when the
 *   lease it names does not exist — the limiter runs before the lookup, so a
 *   flood of guessed lease ids costs the guesser its budget rather than
 *   telling it which guesses were wrong.
 * * **Nothing else is charged.** `heartbeat`, `snapshot` and `release` are
 *   free, which is what keeps a lease from being rate limited into expiring.
 *
 * ## What is not here
 *
 * The events poll (`GET .../hijack/{id}/events`) is not bound. That is stated
 * in the roadmap rather than approximated.
 */

import { extractPromptId, type HijackSession, type InputMode } from "../hub/index.ts";
import { canMutateSession, canReadSession } from "./authorization.ts";
import type { SessionHub } from "./session-hub.ts";
import type { SessionRegistry } from "./session-registry.ts";

/** The refusal every unauthenticated request gets, whatever it asked for. */
export const HIJACK_UNAUTHENTICATED_DETAIL = "authentication required";

/** What the lease routes say about a lease that is gone. */
export const INVALID_LEASE_ERROR = "Invalid or expired hijack session.";

/** What an acquire is refused with when the session is open to everyone. */
export const OPEN_MODE_ERROR = "Hijack not available in open input mode.";

/** What an acquire is refused with when somebody else already holds it. */
export const ALREADY_HIJACKED_ERROR = "Worker is already hijacked.";

/** What every lease route says when the worker's socket has gone. */
export const NO_WORKER_ERROR = "No worker connected for this session.";

/** All a caller over its budget is told. It names no window and no deadline. */
export const RATE_LIMITED_ERROR = "rate_limited";

/**
 * The bucket a caller with no address of its own is charged to.
 *
 * One shared bucket rather than one apiece: a caller the runtime could not
 * name would otherwise be the one caller with no limit.
 */
export const UNKNOWN_CLIENT = "unknown";

/** What a worker id may be, as the reference's path pattern has it. */
const WORKER_ID = /^[\w-]+$/;

/** What a hijack id may be. A UUID's alphabet, and nothing wider. */
const HIJACK_ID = /^[0-9a-f-]{1,64}$/;

/** How long a hijack snapshot waits for a fresh screen, in milliseconds. */
export const HIJACK_SNAPSHOT_WAIT_MS = 1500;

/** How long a guarded send waits for its prompt, in milliseconds. */
const SEND_GUARD_TIMEOUT_MS = 1500;

/** How often a guarded send re-reads the screen, in milliseconds. */
const SEND_GUARD_INTERVAL_MS = 100;

/** As much of a caller as a lease decision reads. */
export interface HijackPrincipal {
  subject_id: string;
  roles: ReadonlySet<string>;
  scopes: ReadonlySet<string>;
  admin_session_scope?: string | null | undefined;
}

/** What the lease routes are served from. */
export interface HijackRouteOptions {
  hub: SessionHub;
  registry: SessionRegistry;
  /** Who is asking, already resolved. */
  principal: HijackPrincipal;
  /** Whether the caller presented a credential that verified. */
  authenticated: boolean;
  /**
   * Where the request came from, as the socket reports it.
   *
   * The connection's own address and never a forwarded header: behind a proxy
   * this collapses every caller into one bucket, which is a worse limit but a
   * real one — a header a client writes is a budget a client picks. The
   * reference says the same thing in the same place, and for the same reason.
   */
  clientAddress?: string | undefined;
}

/** One refusal, in the shape the framework — and so the gate — emits. */
function detail(status: number, message: string, headers: Record<string, string> = {}): Response {
  return Response.json({ detail: message }, { status, headers });
}

/** One refusal, in the shape the lease routes emit. */
function leaseError(status: number, message: string): Response {
  return Response.json({ error: message }, { status });
}

/** The parts of a `/worker/...` path, or nothing when it is not one. */
interface HijackPath {
  workerId: string;
  /** Absent for `acquire`, which names no lease because it creates one. */
  hijackId?: string;
  verb: string;
}

/**
 * Read a `/worker/{id}/hijack/...` path.
 *
 * Returns nothing for a path that is not one at all — the caller falls
 * through to the rest of the application rather than answering for it.
 */
export function parseHijackPath(path: string): HijackPath | undefined {
  const segments = path.split("/");
  // Leading empty segment, `worker`, the id, `hijack`, then one or two more.
  if (segments.length < 5 || segments[0] !== "" || segments[1] !== "worker" || segments[3] !== "hijack") {
    return undefined;
  }
  const workerId = segments[2] as string;
  if (segments.length === 5) {
    return { workerId, verb: segments[4] as string };
  }
  if (segments.length === 6) {
    return { workerId, hijackId: segments[4] as string, verb: segments[5] as string };
  }
  return undefined;
}

/** Whether a verb reads the session rather than driving it. */
const READ_VERBS: ReadonlySet<string> = new Set(["snapshot"]);

/** The verbs that name a lease, and the method each is called with. */
const LEASED_VERBS: ReadonlyMap<string, string> = new Map([
  ["heartbeat", "POST"],
  ["snapshot", "GET"],
  ["send", "POST"],
  ["step", "POST"],
  ["release", "POST"],
]);

/** What one charged verb spends from, and what it counts when it is refused. */
interface RatePolicy {
  /** Spend one token, and say whether there was one. */
  charge(hub: SessionHub, clientId: string): boolean;
  /** The counter a refusal increments. Per verb, though two share a budget. */
  metric: string;
}

/**
 * The verbs that cost something, and what each costs it against.
 *
 * `send` and `step` spend from one budget under two counters: they are the
 * two ways of driving a held terminal, and a caller throttled on one that
 * could carry on through the other would not be throttled at all.
 */
const RATE_LIMITED: ReadonlyMap<string, RatePolicy> = new Map([
  [
    "acquire",
    {
      charge: (hub: SessionHub, clientId: string) => hub.limiter.allowRestAcquire(clientId),
      metric: "rest_acquire_rate_limited_total",
    },
  ],
  [
    "send",
    {
      charge: (hub: SessionHub, clientId: string) => hub.limiter.allowRestSend(clientId),
      metric: "rest_send_rate_limited_total",
    },
  ],
  [
    "step",
    {
      charge: (hub: SessionHub, clientId: string) => hub.limiter.allowRestSend(clientId),
      metric: "rest_step_rate_limited_total",
    },
  ],
]);

/** Which bucket a request is charged to, given the address it arrived from. */
function clientIdOf(address: string | undefined): string {
  return address === undefined || address === "" ? UNKNOWN_CLIENT : address;
}

/**
 * Charge this request against its budget, and refuse it when it is over.
 *
 * @returns The refusal, or nothing when the verb is free or the caller had a
 *   token left.
 */
function overBudget(verb: string, options: HijackRouteOptions): Response | undefined {
  const policy = RATE_LIMITED.get(verb);
  if (policy === undefined) {
    return undefined;
  }
  if (policy.charge(options.hub, clientIdOf(options.clientAddress))) {
    return undefined;
  }
  options.hub.store.metric(policy.metric);
  return leaseError(429, RATE_LIMITED_ERROR);
}

/** The body of a request, or an empty object when there is not one. */
export async function readJsonBody(request: Request): Promise<Record<string, unknown>> {
  try {
    const parsed: unknown = await request.json();
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    // A body that is absent or malformed is the same as one that said
    // nothing: every field below has a default, and the reference's model
    // validates an empty payload for exactly that reason.
    return {};
  }
}

/**
 * Answer a lease request, or hand the path back untouched.
 *
 * @returns The response, or `undefined` when the path belongs to somebody
 *   else.
 */
export async function handleHijackRequest(
  request: Request,
  options: HijackRouteOptions,
): Promise<Response | undefined> {
  const url = new URL(request.url);
  const parsed = parseHijackPath(url.pathname);
  if (parsed === undefined) {
    return undefined;
  }
  return await answer(request, parsed, options);
}

/** The refusal a path's own grammar earns, or nothing when it is well-formed. */
function malformed(parsed: HijackPath): Response | undefined {
  if (!WORKER_ID.test(parsed.workerId)) {
    return detail(422, "invalid route path parameters");
  }
  if (parsed.hijackId !== undefined && !HIJACK_ID.test(parsed.hijackId)) {
    return detail(422, "invalid route path parameters");
  }
  return undefined;
}

/** The method a verb is called with, or nothing when no verb matches. */
function methodFor(parsed: HijackPath): string | undefined {
  if (parsed.hijackId === undefined) {
    return parsed.verb === "acquire" ? "POST" : undefined;
  }
  return LEASED_VERBS.get(parsed.verb);
}

/** One lease request, once its path has been read. */
async function answer(request: Request, parsed: HijackPath, options: HijackRouteOptions): Promise<Response> {
  const bad = malformed(parsed);
  if (bad !== undefined) {
    return bad;
  }
  const expected = methodFor(parsed);
  if (expected === undefined) {
    // A verb nobody defined is not a route at all, which is a 404 rather than
    // a refusal — the same answer the reference's router reaches.
    return detail(404, "Not Found");
  }
  if (request.method.toUpperCase() !== expected) {
    return detail(405, "Method Not Allowed", { Allow: expected });
  }

  // The gate, in its order: authenticate, then find the session, then decide
  // what this caller may do to it. Existence comes second so that nobody can
  // enumerate session ids by watching whether the refusal is 401 or 404.
  if (!options.authenticated) {
    return detail(401, HIJACK_UNAUTHENTICATED_DETAIL);
  }
  const session = options.registry.definition(parsed.workerId);
  if (session === undefined) {
    return detail(404, `unknown session: ${parsed.workerId}`);
  }
  const allowed = READ_VERBS.has(parsed.verb)
    ? canReadSession(options.principal, session)
    : canMutateSession(options.principal, session, "session.control.hijack");
  if (!allowed) {
    return detail(403, "insufficient privileges");
  }

  // After the gate and before anything is looked up: a caller who cannot get
  // past the gate never spends a token, and a caller who is over the budget
  // is told so rather than told whether the lease they named exists.
  const refused = overBudget(parsed.verb, options);
  if (refused !== undefined) {
    return refused;
  }

  if (parsed.hijackId === undefined) {
    return await acquire(request, parsed.workerId, options);
  }
  return await throughLease(request, parsed.workerId, parsed.hijackId, parsed.verb, options);
}

/** Take the lease. */
async function acquire(request: Request, workerId: string, options: HijackRouteOptions): Promise<Response> {
  const hub = options.hub;
  const body = await readJsonBody(request);
  // The reference's model defaults: a caller that names neither is an
  // `operator` holding the lease for ninety seconds.
  const owner = String(body.owner ?? "operator");
  const leaseSeconds = hub.clampLease(Number(body.lease_s ?? 90));

  await hub.lease.cleanupExpired(workerId);
  const hijackId = crypto.randomUUID();
  const wallNow = hub.wallNow();
  const result = await hub.lease.tryAcquireRest(workerId, {
    owner,
    leaseSeconds,
    hijackId,
    now: hub.monotonic(),
  });
  if (!result.ok) {
    if (result.reason !== "already_hijacked") {
      // The pause reached a worker that was not there, or was not sent at
      // all. Either way the worker must not be left believing it is held.
      await hub.sendWorker(workerId, {
        type: "control",
        action: "resume",
        owner,
        lease_s: 0,
        hijack_id: hijackId,
        ts: wallNow,
      });
    }
    return leaseError(409, refusalFor(result.reason));
  }
  hub.store.metric("hijack_acquires_total");
  hub.store.notifyHijackChanged(workerId, { enabled: true, owner });
  await hub.appendEvent(workerId, "hijack_acquired", { hijack_id: hijackId, owner, lease_s: leaseSeconds });
  await hub.router.broadcastHijackState(workerId);
  // The *authenticated* subject, which is not the self-declared `owner`
  // label: release checks ownership against this one. Present, because the
  // acquire above committed it and nothing has awaited since — the same
  // reading the reference marks `pragma: no branch`.
  const held = (await hub.lease.getRestSession(workerId, hijackId)) as HijackSession;
  held.acquiredBy = options.principal.subject_id;
  return Response.json({
    ok: true,
    worker_id: workerId,
    hijack_id: hijackId,
    lease_expires_at: wallNow + leaseSeconds,
    owner,
  });
}

/** What a refused acquire is told, by the reason the lease manager gave. */
function refusalFor(reason?: string): string {
  if (reason === "already_hijacked") {
    return ALREADY_HIJACKED_ERROR;
  }
  if (reason === "open_mode") {
    return OPEN_MODE_ERROR;
  }
  return NO_WORKER_ERROR;
}

/** Every route that acts through a lease somebody already holds. */
async function throughLease(
  request: Request,
  workerId: string,
  hijackId: string,
  verb: string,
  options: HijackRouteOptions,
): Promise<Response> {
  const hub = options.hub;
  const held = await hub.lease.getRestSession(workerId, hijackId);
  if (held === undefined) {
    return leaseError(404, INVALID_LEASE_ERROR);
  }
  if (verb === "heartbeat") {
    return await heartbeat(request, workerId, hijackId, held.owner, hub);
  }
  if (verb === "snapshot") {
    return await snapshot(workerId, hijackId, held.leaseExpiresAt, hub);
  }
  if (verb === "send") {
    return await send(request, workerId, hijackId, held.leaseExpiresAt, hub);
  }
  if (verb === "step") {
    return await step(workerId, hijackId, held.owner, held.leaseExpiresAt, hub);
  }
  return await release(workerId, hijackId, held, options);
}

/** Keep the lease alive. */
async function heartbeat(
  request: Request,
  workerId: string,
  hijackId: string,
  owner: string,
  hub: SessionHub,
): Promise<Response> {
  const body = await readJsonBody(request);
  const leaseSeconds = hub.clampLease(Number(body.lease_s ?? 90));
  const expires = await hub.lease.extendLease(workerId, hijackId, owner, leaseSeconds, hub.monotonic());
  if (expires === undefined) {
    return leaseError(404, INVALID_LEASE_ERROR);
  }
  await hub.appendEvent(workerId, "hijack_heartbeat", { hijack_id: hijackId, lease_s: leaseSeconds });
  await hub.router.broadcastHijackState(workerId);
  return Response.json({
    ok: true,
    worker_id: workerId,
    hijack_id: hijackId,
    lease_expires_at: hub.monoToWall(expires),
  });
}

/** Read the screen through the lease. */
async function snapshot(
  workerId: string,
  hijackId: string,
  fallbackExpires: number,
  hub: SessionHub,
): Promise<Response> {
  const screen = await hub.polling.waitForSnapshot(workerId, HIJACK_SNAPSHOT_WAIT_MS);
  // Re-read the expiry: a concurrent heartbeat may have extended it while the
  // poll above was waiting, and answering with the stale one would tell the
  // holder their lease is shorter than it is.
  const expires = await hub.lease.getFreshExpiry(workerId, hijackId, fallbackExpires);
  return Response.json({
    ok: true,
    worker_id: workerId,
    hijack_id: hijackId,
    snapshot: screen ?? null,
    prompt_id: extractPromptId(screen) ?? null,
    lease_expires_at: hub.monoToWall(expires),
  });
}

/** Type through the lease. */
async function send(
  request: Request,
  workerId: string,
  hijackId: string,
  fallbackExpires: number,
  hub: SessionHub,
): Promise<Response> {
  const body = await readJsonBody(request);
  const keys = String(body.keys ?? "");
  if (keys === "") {
    return leaseError(400, "keys must not be empty.");
  }
  if (keys.length > hub.maxInputChars) {
    return leaseError(400, `keys too long: ${keys.length} > ${hub.maxInputChars}`);
  }
  const guard = await hub.polling.waitForGuard(workerId, {
    ...(typeof body.expect_prompt_id === "string" ? { expectPromptId: body.expect_prompt_id } : {}),
    ...(typeof body.expect_regex === "string" ? { expectRegex: body.expect_regex } : {}),
    timeoutMs: Number(body.timeout_ms ?? SEND_GUARD_TIMEOUT_MS),
    pollIntervalMs: Number(body.poll_interval_ms ?? SEND_GUARD_INTERVAL_MS),
  });
  if (!guard.matched) {
    return Response.json(
      {
        // The coordinator names its own reason on every refusal — the guard
        // it would not compile and the screen that never arrived alike — so
        // there is nothing here to stand in for.
        error: guard.reason as string,
        current_prompt_id: extractPromptId(guard.snapshot) ?? null,
      },
      { status: 409 },
    );
  }
  // Re-checked after the wait: a lease can lapse or be replaced inside the
  // poll window, and keystrokes must never be sent on a lease that is gone.
  if (!(await hub.lease.checkValid(workerId, hijackId))) {
    return leaseError(404, INVALID_LEASE_ERROR);
  }
  if (!(await hub.sendWorker(workerId, { type: "input", data: keys, ts: hub.wallNow() }))) {
    return leaseError(409, NO_WORKER_ERROR);
  }
  await hub.appendEvent(workerId, "hijack_send", { hijack_id: hijackId, keys: keys.slice(0, 120) });
  return Response.json({
    ok: true,
    worker_id: workerId,
    hijack_id: hijackId,
    sent: keys,
    matched_prompt_id: extractPromptId(guard.snapshot) ?? null,
    lease_expires_at: hub.monoToWall(await hub.lease.getFreshExpiry(workerId, hijackId, fallbackExpires)),
  });
}

/** Let the paused worker past one checkpoint. */
async function step(
  workerId: string,
  hijackId: string,
  owner: string,
  fallbackExpires: number,
  hub: SessionHub,
): Promise<Response> {
  if (!(await hub.lease.checkValid(workerId, hijackId))) {
    return leaseError(404, INVALID_LEASE_ERROR);
  }
  const sent = await hub.sendWorker(workerId, {
    type: "control",
    action: "step",
    owner,
    lease_s: 0,
    ts: hub.wallNow(),
  });
  if (!sent) {
    return leaseError(409, NO_WORKER_ERROR);
  }
  await hub.appendEvent(workerId, "hijack_step", { hijack_id: hijackId });
  hub.store.metric("hijack_steps_total");
  return Response.json({
    ok: true,
    worker_id: workerId,
    hijack_id: hijackId,
    lease_expires_at: hub.monoToWall(await hub.lease.getFreshExpiry(workerId, hijackId, fallbackExpires)),
  });
}

/** Give the lease back. */
async function release(
  workerId: string,
  hijackId: string,
  held: HijackSession,
  options: HijackRouteOptions,
): Promise<Response> {
  const hub = options.hub;
  const owner = held.owner;
  // Only whoever took the lease may drop it — not every operator who can
  // reach the route. A lease with no acquirer recorded keeps the older
  // model: possession of the unguessable id is the capability.
  if (held.acquiredBy !== undefined && held.acquiredBy !== options.principal.subject_id) {
    return leaseError(403, "Not the lease owner.");
  }
  const { ok, shouldResume } = await hub.lease.releaseRest(workerId, hijackId);
  if (!ok) {
    return leaseError(404, INVALID_LEASE_ERROR);
  }
  // Re-checked: a concurrent acquire may have taken the worker between the
  // release above and the resume below, and resuming then would unpause a
  // session somebody else is now driving.
  if (shouldResume && !(await hub.lease.stillHijacked(workerId))) {
    await hub.sendWorker(workerId, { type: "control", action: "resume", owner, lease_s: 0, ts: hub.wallNow() });
  }
  // Always, whether or not a resume frame went out: this lease is gone either
  // way, and a subscriber left believing otherwise refuses the next acquire.
  hub.store.notifyHijackChanged(workerId, { enabled: false });
  hub.store.metric("hijack_releases_total");
  await hub.appendEvent(workerId, "hijack_released", { hijack_id: hijackId, owner });
  await hub.router.broadcastHijackState(workerId);
  await hub.pruneIfIdle(workerId);
  return Response.json({ ok: true, worker_id: workerId, hijack_id: hijackId });
}

/** The input modes a session may be put into, for the mode route to check. */
export const INPUT_MODES: ReadonlySet<string> = new Set<InputMode>(["open", "hijack"]);
