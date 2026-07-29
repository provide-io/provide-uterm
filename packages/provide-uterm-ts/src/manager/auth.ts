//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The swarm manager's token boundary.
 *
 * Port of `provide.uterm.manager.auth`. The manager spawns and kills
 * processes across a fleet, so this is the line between an operator and a
 * worker that has been taken over.
 *
 * * **Two privilege levels.** The operator token authorizes every route. The
 *   worker tokens authorize only the two self-report routes — status and
 *   register — and are refused on spawn, kill, prune and every read.
 * * **A worker's token is bound to its own agent id.** The fleet secret is an
 *   HMAC key that never leaves the manager; each worker is given
 *   `HMAC(secret, its own id)`. The derivation is one-way, so a worker cannot
 *   compute another's, and the presented token is checked against the id *in
 *   the path* — which is what stops one compromised worker reporting as, or
 *   registering over, any other.
 * * **The route patterns are fully anchored**, so a near-miss never reaches
 *   the low-privilege branch.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

/** The two routes a worker may call about itself, and only by POST. */
const SELF_REPORT_ROUTES: ReadonlyArray<readonly [string, RegExp]> = [
  ["POST", /^\/agent\/([^/]+)\/status$/],
  ["POST", /^\/agent\/([^/]+)\/register$/],
];

/** What an unauthorized HTTP caller is told. */
export const UNAUTHORIZED_STATUS = 401;

/**
 * The body an unauthorized caller gets.
 *
 * It says only that the call was unauthorized: a caller learning *which* of
 * the two tokens it failed is a caller being told what to try next.
 */
export const UNAUTHORIZED_BODY = '{"error":"Unauthorized"}';

/**
 * The close code an unauthorized WebSocket gets.
 *
 * The socket is accepted and then closed, because a WebSocket has no status
 * code until the handshake completes — refusing before it would leave the
 * caller with no reason at all.
 */
export const WEBSOCKET_CLOSE_CODE = 4403;

/** How the manager is configured to check tokens. */
export interface TokenAuthConfig {
  /** The operator token, which authorizes everything. */
  token: string;
  /** The fleet-shared worker token, if one is configured. */
  workerToken?: string | undefined;
  /** The HMAC secret each worker's own token is derived from. */
  workerSecret?: string | undefined;
  /** Whether the fleet-shared token is refused on the self-report routes. */
  enforcePerAgentWorkerToken?: boolean | undefined;
  /** Exact paths needing no token. */
  publicPaths?: ReadonlySet<string> | undefined;
  /** Path prefixes needing no token. */
  publicPrefixes?: readonly string[] | undefined;
}

/** As much of an ASGI scope as this decision needs. */
export interface AsgiScope {
  type: string;
  method?: string | undefined;
  path?: string | undefined;
  queryString?: string | undefined;
  headers?: ReadonlyArray<readonly [string, string]> | undefined;
}

/** Whether a request may proceed, and how to refuse it if not. */
export type AuthDecision = { allow: true } | { allow: false; kind: "http" | "websocket" };

/**
 * The token a worker uses to report about itself.
 *
 * `"sha256=" + HMAC-SHA256(secret, agentId)`. A stable wire contract: workers
 * hold the derived value, so changing the format means migrating the fleet.
 */
export function deriveAgentToken(secret: string, agentId: string): string {
  return `sha256=${createHmac("sha256", secret).update(agentId, "utf8").digest("hex")}`;
}

/**
 * The agent id a self-report route names, if this is one.
 *
 * Matched against the whole path, so `/agent/x/statusfoo`, a nested id, a
 * trailing slash and a query string all miss — and therefore never reach the
 * branch where a worker token is accepted.
 */
export function extractSelfReportAgentId(path: string, method: string): string | undefined {
  for (const [expected, pattern] of SELF_REPORT_ROUTES) {
    if (method !== expected) {
      continue;
    }
    const match = pattern.exec(path);
    if (match !== null) {
      return match[1] as string;
    }
  }
  return undefined;
}

/** Compare two tokens without letting the time taken say how much matched. */
function tokensMatch(provided: string, expected: string): boolean {
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  // `timingSafeEqual` raises on a length mismatch rather than answering; the
  // length is not what is being protected.
  if (a.length !== b.length) {
    return false;
  }
  // Not `provided === expected`: the two answer alike, and no test can tell
  // them apart. What differs is how long the wrong answer takes, which is how
  // a token gets guessed one character at a time.
  return timingSafeEqual(a, b);
}

/** Whether a path needs no token at all. */
export function isPublicPath(config: TokenAuthConfig, path: string): boolean {
  if (config.publicPaths?.has(path) === true) {
    return true;
  }
  return (config.publicPrefixes ?? []).some((prefix) => path.startsWith(prefix));
}

/**
 * Whether `provided` may call this route.
 *
 * The operator token authorizes everything. On a self-report route the
 * worker's own derived token is accepted — bound to the agent id in the path
 * — and the fleet-shared token as well, unless per-agent enforcement is on.
 * An operator route never accepts either worker token.
 */
export function isAuthorized(config: TokenAuthConfig, provided: string, path: string, method: string): boolean {
  if (tokensMatch(provided, config.token)) {
    return true;
  }
  const agentId = extractSelfReportAgentId(path, method);
  if (agentId === undefined) {
    return false;
  }
  if (config.workerSecret !== undefined && tokensMatch(provided, deriveAgentToken(config.workerSecret, agentId))) {
    return true;
  }
  // A shared token cannot be bound to one agent, so accepting it means any
  // worker can report as any other. Kept for workers not yet migrated, and
  // closed by the setting.
  if (config.enforcePerAgentWorkerToken !== true && config.workerToken !== undefined) {
    return tokensMatch(provided, config.workerToken);
  }
  return false;
}

/** The token on a request, and whether the request skips the check entirely. */
export function extractRequestToken(scope: AsgiScope): { token: string; passThrough: boolean } {
  if (scope.type === "websocket") {
    // A WebSocket handshake carries no headers this can rely on, so the token
    // travels in the query.
    const params = new URLSearchParams(scope.queryString ?? "");
    return { token: (params.get("token") ?? "").trim(), passThrough: false };
  }
  if (scope.method === "OPTIONS") {
    // A browser cannot attach a token to a preflight.
    return { token: "", passThrough: true };
  }
  const headers = new Map(scope.headers ?? []);
  const authorization = headers.get("authorization") ?? "";
  if (authorization.startsWith("Bearer ")) {
    return { token: authorization.slice("Bearer ".length).trim(), passThrough: false };
  }
  return { token: (headers.get("x-api-token") ?? "").trim(), passThrough: false };
}

/**
 * Whether a request may reach the application behind this boundary.
 *
 * Anything that is not an HTTP request or a WebSocket — lifespan traffic, for
 * instance — is not somebody calling, and passes through.
 */
export function authorizeScope(config: TokenAuthConfig, scope: AsgiScope): AuthDecision {
  if (scope.type !== "http" && scope.type !== "websocket") {
    return { allow: true };
  }
  const path = scope.path ?? "";
  if (isPublicPath(config, path)) {
    return { allow: true };
  }
  const { token, passThrough } = extractRequestToken(scope);
  if (passThrough) {
    return { allow: true };
  }
  if (isAuthorized(config, token, path, scope.method ?? "")) {
    return { allow: true };
  }
  return { allow: false, kind: scope.type === "websocket" ? "websocket" : "http" };
}
