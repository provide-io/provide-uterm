//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The chokepoint every MCP tool call passes through.
 *
 * Port of `provide.uterm.ai.auth`. The table saying which role each tool needs
 * lives in `./policy`; this is what happens when a call actually arrives.
 *
 * **A principal holding several roles is judged on the best of them.** The
 * check asks whether *any* role held meets the minimum, so somebody who is an
 * operator and an admin gets an admin's reach. Somebody holding none meets
 * nothing at all — including `viewer`, which is the point: an empty set is not
 * a quiet grant of the lowest tier.
 *
 * **A refusal is a result, not an exception.** It has the same shape as every
 * other tool answer so a caller branches on it rather than special-casing, and
 * it names the tool, the role required and the roles held — which is what an
 * operator needs in order to fix the grant.
 */

import type { AuthInfo } from "@modelcontextprotocol/sdk/server/auth/types.js";
import { type Role, requiredRole, roleAtLeast, roleRank } from "./policy.ts";

/** Who is calling a tool. */
export interface McpPrincipal {
  subjectId: string;
  roles: readonly string[];
}

/**
 * The principal a server falls back to.
 *
 * Anonymous, holding only `viewer` — the least this can be while still being
 * able to read anything at all.
 */
export const DEFAULT_PRINCIPAL: McpPrincipal = { subjectId: "anonymous", roles: ["viewer"] };

/**
 * Whether this principal may invoke something needing `minimum`.
 *
 * Any one role held is enough; they are alternatives. Holding none is enough
 * for nothing.
 */
export function hasAtLeast(principal: McpPrincipal, minimum: Role): boolean {
  return principal.roles.some((role) => roleAtLeast(role, minimum));
}

/**
 * The most privileged role a principal holds, for display.
 *
 * **Not** what any decision is made on, and it can name a role the principal
 * cannot use: somebody holding only `superuser` gets `superuser` here and
 * meets no requirement at all, and somebody holding nothing gets `viewer`
 * while meeting nothing. Authorization asks {@link hasAtLeast}, which asks the
 * ladder.
 */
export function primaryRole(principal: McpPrincipal): string {
  if (principal.roles.length === 0) {
    return "viewer";
  }
  // The reference's `max(..., key=role_rank)`, keeping the first of equals.
  // Ties are reachable — every unrecognised role ranks the same — but the
  // reference holds its roles in a `frozenset`, so which of two tied roles it
  // names depends on hash order. This is the port's own answer, not a matched
  // one.
  let best = principal.roles[0] as string;
  for (const role of principal.roles.slice(1)) {
    if (roleRank(role) > roleRank(best)) {
      best = role;
    }
  }
  return best;
}

/** Why a call was refused, in the shape every tool answers with. */
export interface AuthorizationDenial {
  success: false;
  error: "authorization_denied";
  tool: string;
  required_role: Role;
  principal: string;
  principal_roles: string[];
}

/** Render a refusal as a tool result. */
export function denyPayload(tool: string, principal: McpPrincipal, required: Role): AuthorizationDenial {
  return {
    success: false,
    error: "authorization_denied",
    tool,
    required_role: required,
    principal: principal.subjectId,
    // Sorted, so two servers refusing the same call say the same thing.
    principal_roles: [...principal.roles].sort(),
  };
}

/** The transport's authenticated-request data, if any. */
export interface RequestContext {
  /**
   * The SDK's per-call auth info, or nothing on an unauthenticated
   * transport. May throw, which is treated as nothing — an unbound
   * context answers "no identity", not an error.
   */
  getAuthInfo(): AuthInfo | undefined;
}

/**
 * Who is calling: the transport's authenticated identity, or the server's
 * default.
 *
 * A context that fails to answer is treated as having said nothing rather
 * than as an error — a broken lookup falls back to the default and cannot
 * become a privilege. Roles always come from `fallback`: the transport
 * binds identity, never authorization. `AuthInfo` has no first-class
 * issuer/subject; a token verifier that supplies them stashes them in
 * `extra.iss` / `extra.sub`. A component the verifier does not supply
 * degrades to `null`, mirroring the Python port's
 * `principal_components()`.
 */
export async function resolvePrincipal(
  context: RequestContext | undefined,
  fallback: McpPrincipal = DEFAULT_PRINCIPAL,
): Promise<McpPrincipal> {
  if (context === undefined) {
    return fallback;
  }
  let authInfo: AuthInfo | undefined;
  try {
    authInfo = context.getAuthInfo();
  } catch {
    return fallback;
  }
  if (authInfo === undefined) {
    return fallback;
  }
  const issuer = (authInfo.extra?.iss as string | undefined) ?? null;
  const subject = (authInfo.extra?.sub as string | undefined) ?? null;
  return { subjectId: JSON.stringify([authInfo.clientId, issuer, subject]), roles: fallback.roles };
}

/** What a guarded call produced: the tool's answer, or the refusal. */
export type ToolOutcome<T> = { allowed: true; result: T } | { allowed: false; denial: AuthorizationDenial };

/**
 * Run a tool only if the caller may.
 *
 * The role is looked up by name, so a tool with no entry in the table raises
 * rather than running — which is what stops a newly added tool from slipping
 * through unguarded.
 *
 * @throws {Error} When the tool has no policy registered.
 */
export async function authorized<T>(
  tool: string,
  context: RequestContext | undefined,
  run: (principal: McpPrincipal) => Promise<T>,
  fallback: McpPrincipal = DEFAULT_PRINCIPAL,
): Promise<ToolOutcome<T>> {
  // Looked up first, so a tool with no policy fails before a principal is
  // even resolved.
  const minimum = requiredRole(tool);
  const principal = await resolvePrincipal(context, fallback);
  if (!hasAtLeast(principal, minimum)) {
    return { allowed: false, denial: denyPayload(tool, principal, minimum) };
  }
  return { allowed: true, result: await run(principal) };
}
