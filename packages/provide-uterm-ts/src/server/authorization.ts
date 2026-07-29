//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What an authenticated principal is allowed to do.
 *
 * Port of `provide.uterm.server.authorization.LocalAuthorizationProvider` —
 * the standard RBAC provider, which is the one a deployment gets unless it
 * configures a webhook policy engine.
 *
 * Authentication says who a caller is; this says what that buys them. The two
 * are separate because they fail differently: an unauthenticated caller is a
 * 401 and an unauthorised one is a 403, and telling a caller which of the two
 * happened is the difference between "log in" and "ask someone".
 */

/** A capability is a named thing a principal may do. */
export type Capability = string;

/**
 * What each role grants.
 *
 * A closed table rather than a hierarchy: `operator` is not `viewer` plus
 * more by construction, it is its own set. A hierarchy is where a capability
 * added to the bottom silently reaches the top.
 */
export const ROLE_CAPABILITIES: Readonly<Record<string, readonly Capability[]>> = {
  viewer: ["session.read", "session.recording.read", "graphical.target.read"],
  operator: [
    "session.read",
    "session.recording.read",
    "session.control.create",
    "session.control.connect",
    "session.control.mode",
    "session.control.clear",
    "session.control.update",
    "graphical.target.read",
    "graphical.target.manage",
    "graphical.session.attach",
  ],
  admin: [
    "session.read",
    "session.recording.read",
    "session.control.create",
    "session.control.connect",
    "session.control.mode",
    "session.control.clear",
    "session.control.update",
    "session.control.delete",
    "session.control.hijack",
    "graphical.target.read",
    "graphical.target.manage",
    "graphical.session.attach",
  ],
};

/** As much of a principal as an authorization decision reads. */
export interface AuthorizablePrincipal {
  subject_id: string;
  roles: ReadonlySet<string>;
  scopes: ReadonlySet<string>;
  /**
   * The one session an admin grant is confined to, if it is confined.
   *
   * A tunnel share can hand out operator rights over a single session. Such a
   * principal holds the `admin` role and must still fail a *global* admin
   * check, or the grant would reach every other session on the server.
   */
  admin_session_scope?: string | null | undefined;
}

/** As much of a session as an authorization decision reads. */
export interface AuthorizableSession {
  session_id: string;
  owner: string | null;
  visibility: string;
}

/**
 * Every capability a principal holds.
 *
 * Scopes narrow rather than widen: a token carrying scopes is cut down to the
 * intersection of its roles and its scopes, so a scoped token can never do
 * more than its roles allow. `*` opts out of the narrowing.
 */
export function capabilitiesFor(principal: AuthorizablePrincipal): ReadonlySet<Capability> {
  const granted = new Set<Capability>();
  for (const role of principal.roles) {
    for (const capability of ROLE_CAPABILITIES[role] ?? []) {
      granted.add(capability);
    }
  }
  if (principal.scopes.size > 0 && !principal.scopes.has("*")) {
    return new Set([...granted].filter((capability) => principal.scopes.has(capability)));
  }
  return granted;
}

/** Whether a principal holds one capability. */
export function hasCapability(principal: AuthorizablePrincipal, capability: Capability): boolean {
  return capabilitiesFor(principal).has(capability);
}

/** Whether a principal is an administrator of the whole server. */
export function isAdmin(principal: AuthorizablePrincipal): boolean {
  return principal.roles.has("admin") && (principal.admin_session_scope ?? null) === null;
}

/** Whether a principal is an administrator of one particular session. */
export function isAdminForSession(principal: AuthorizablePrincipal, session: AuthorizableSession): boolean {
  if (!principal.roles.has("admin")) {
    return false;
  }
  const scope = principal.admin_session_scope ?? null;
  return scope === null || scope === session.session_id;
}

/** Whether a principal owns a session. A session with no owner is owned by nobody. */
export function isOwner(principal: AuthorizablePrincipal, session: AuthorizableSession): boolean {
  return session.owner !== null && session.owner === principal.subject_id;
}

/**
 * Whether a principal may read a session's terminal data.
 *
 * The capability is checked first and on its own, so a scoped token that lost
 * `session.read` is refused however administrative its roles look.
 */
export function canReadSession(principal: AuthorizablePrincipal, session: AuthorizableSession): boolean {
  if (!hasCapability(principal, "session.read")) {
    return false;
  }
  if (isAdminForSession(principal, session) || isOwner(principal, session)) {
    return true;
  }
  // A share link's principal is named after the session it was minted for, so
  // it reads that session and nothing else.
  if (principal.subject_id.startsWith(`share:${session.session_id}:`)) {
    return true;
  }
  if (session.visibility === "public") {
    return true;
  }
  if (session.visibility === "operator") {
    return principal.roles.has("operator");
  }
  // Anything else — `private`, or a visibility nobody defined — is closed.
  return false;
}

/**
 * Whether a principal may perform one mutating action on a session.
 *
 * Deliberately *not* symmetric with {@link canReadSession}: visibility does
 * not appear at all. A session being public says who may watch it, never who
 * may change it — so a public session is readable by anyone with the
 * capability and still writable only by its owner or an administrator of it.
 *
 * An unowned session therefore admits nobody but an admin. That is the
 * reference's reading and it is the safe one: a session with no owner has
 * nobody whose consent a change could be attributed to.
 */
export function canMutateSession(
  principal: AuthorizablePrincipal,
  session: AuthorizableSession,
  action: Capability,
): boolean {
  // The capability first and on its own, so a scoped token that lost the
  // action is refused however administrative its roles look.
  if (!hasCapability(principal, action)) {
    return false;
  }
  if (isAdminForSession(principal, session)) {
    return true;
  }
  return isOwner(principal, session);
}
