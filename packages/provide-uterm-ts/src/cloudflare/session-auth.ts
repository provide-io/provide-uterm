//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * How a Durable Object decides what a caller may do.
 *
 * Port of the auth mixin in
 * `provide.uterm.cloudflare.do.session_runtime.auth`. A share cookie first,
 * then a JWT, then the session's own ownership.
 *
 * The reference reaches these through `self`; here the state a decision needs
 * is a parameter, which is what makes each of them assertable on its own.
 */

import { verifyToken } from "../serverauth/index.ts";
import type { JwtConfig } from "./config.ts";
import { extractBearerOrCookie, JwtValidationError, type Principal, resolveRole } from "./jwt.ts";

/** The roles a share cookie can carry. */
const CONTROL_ROLE = "admin";
const SHARE_ROLE = "viewer";

/** What a caller gets with no usable credential at all. */
const FALLBACK_ROLE = "viewer";

/** The auth modes that admit everybody. */
const OPEN_MODES: ReadonlySet<string> = new Set(["none", "dev"]);

/** The cookie a share link sets, named for the session it is for. */
export function shareCookieName(workerId: string): string {
  return `uterm_tunnel_${workerId}`;
}

/** What the session knows about itself. */
export interface SessionAuthContext {
  workerId: string;
  jwt: JwtConfig;
  /** The digest of the control token, if one was issued. */
  controlTokenHash?: string | undefined;
  /** The digest of the share token, if one was issued. */
  shareTokenHash?: string | undefined;
  /** When the tunnel's tokens stop authorising, in seconds. */
  expiresAt?: number | undefined;
  /** The address the tokens were issued to, when the tunnel binds them. */
  issuedIp?: string | undefined;
  /** Whether tokens are bound to the address that requested them. */
  ipBinding?: boolean | undefined;
  /** The session's owner, if it has one. */
  owner?: string | undefined;
}

/** Read one cookie from a request's `Cookie` header. */
export function readCookie(header: string, name: string): string | undefined {
  for (const part of header.split(";")) {
    const trimmed = part.trim();
    const separator = trimmed.indexOf("=");
    if (separator === -1) {
      continue;
    }
    if (trimmed.slice(0, separator).trim() === name) {
      const value = trimmed.slice(separator + 1).trim();
      if (value !== "") {
        return value;
      }
    }
  }
  return undefined;
}

/** A request, as much of one as these decisions read. */
export interface AuthRequest {
  headers?: { get(name: string): string | null | undefined } | undefined;
}

/** Read a header, or nothing if the request cannot answer. */
function header(request: AuthRequest, name: string): string {
  try {
    return String(request.headers?.get(name) ?? "");
  } catch {
    return "";
  }
}

/**
 * The role a share cookie grants, if any.
 *
 * The cookie names the session it is for, so a token issued for one session
 * cannot authorise another — a browser holding several is not a browser that
 * may use any of them anywhere.
 *
 * Both the expiry and the address binding are checked here as well as at the
 * Worker, because a Durable Object is reachable directly.
 */
export function shareRoleForRequest(
  request: AuthRequest,
  context: SessionAuthContext,
  now: number,
): string | undefined {
  const cookies = header(request, "cookie") || header(request, "Cookie");
  const token = readCookie(cookies, shareCookieName(context.workerId));
  if (token === undefined) {
    return undefined;
  }

  // Compared against digests, not tokens: the object holds hashes so that a
  // disclosure leaks nothing usable.
  // The presence tests cannot change an answer — an absent digest verifies
  // nothing, because an empty stored hash already refuses — and are here so
  // "no token was issued" reads as a fact about the session rather than as a
  // property of the comparison.
  let role: string | undefined;
  if (context.controlTokenHash !== undefined && verifyToken(token, context.controlTokenHash)) {
    role = CONTROL_ROLE;
  } else if (context.shareTokenHash !== undefined && verifyToken(token, context.shareTokenHash)) {
    role = SHARE_ROLE;
  }
  // Returning early cannot change an answer either: the checks below only
  // ever return undefined, and the function ends by returning this same
  // value. Kept because the checks that follow are about a role that exists,
  // and running them against nothing reads as though they might grant one.
  if (role === undefined) {
    return undefined;
  }

  // Stamped at issuance. A cookie outliving its tunnel would keep a revoked
  // share working against the object directly.
  if (context.expiresAt !== undefined && now > context.expiresAt) {
    return undefined;
  }

  if (context.ipBinding === true) {
    const issued = context.issuedIp ?? "";
    const client = header(request, "CF-Connecting-IP");
    // Only enforced where an address was recorded: a tunnel issued before
    // binding was switched on has nothing to compare against.
    if (issued !== "" && client !== issued) {
      return undefined;
    }
  }
  return role;
}

/** How a caller's token is turned into a principal. */
export type PrincipalDecoder = (token: string, config: JwtConfig) => Promise<Principal>;

/**
 * Raise an owner to operator.
 *
 * A floor, not an assignment: an admin stays admin. Without it an owner
 * holding a viewer token could read their own session through the visibility
 * check and be refused every mutation on it.
 */
export function elevateOwner(jwtRole: string, owner: string | undefined, subject: string): string {
  if (jwtRole === CONTROL_ROLE) {
    return CONTROL_ROLE;
  }
  // An owner recorded as nothing is not an owner, and a subject of nothing
  // does not match one.
  return owner !== undefined && owner !== "" && subject === owner ? "operator" : jwtRole;
}

/**
 * The role a caller acts with.
 *
 * A share cookie wins over a token: it is the narrower grant, issued for this
 * session alone. Anything unreadable falls back to viewer — the token was
 * already validated before this runs, so this is role extraction rather than
 * authentication, and refusing here would turn a role question into a 500.
 */
export async function browserRoleForRequest(
  request: AuthRequest,
  context: SessionAuthContext,
  decode: PrincipalDecoder,
  now: number,
): Promise<string> {
  if (OPEN_MODES.has(context.jwt.mode)) {
    return CONTROL_ROLE;
  }
  const shareRole = shareRoleForRequest(request, context, now);
  if (shareRole !== undefined) {
    return shareRole;
  }
  const token = extractBearerOrCookie(request);
  if (token === undefined) {
    return FALLBACK_ROLE;
  }
  let principal: Principal;
  try {
    principal = await decode(token, context.jwt);
  } catch (error) {
    // Only a validation failure is a viewer. Anything else — a JWKS endpoint
    // that cannot be reached, say — propagates, so the caller answers 5xx
    // rather than silently downgrading somebody who is not a viewer.
    if (error instanceof JwtValidationError) {
      return FALLBACK_ROLE;
    }
    throw error;
  }
  return elevateOwner(resolveRole(principal), context.owner, principal.subjectId);
}

/**
 * The subject a caller is, for the per-session ownership checks.
 *
 * Nothing in an open mode: there is no subject to speak of, and inventing one
 * would make every session look owned.
 */
export async function browserSubjectForRequest(
  request: AuthRequest,
  context: SessionAuthContext,
  decode: PrincipalDecoder,
): Promise<string | undefined> {
  if (OPEN_MODES.has(context.jwt.mode)) {
    return undefined;
  }
  const token = extractBearerOrCookie(request);
  if (token === undefined) {
    return undefined;
  }
  try {
    return (await decode(token, context.jwt)).subjectId;
  } catch (error) {
    if (error instanceof JwtValidationError) {
      return undefined;
    }
    throw error;
  }
}
