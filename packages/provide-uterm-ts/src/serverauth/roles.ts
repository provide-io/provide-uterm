//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The canonical RBAC role allow-list.
 *
 * Port of the Python module `provide.uterm.server.auth_roles`.
 *
 * Roles arrive from a JWT, a proxy header or a webhook IDP — none of which
 * this server controls. Anything outside the allow-list is dropped, so a
 * compromised issuer cannot mint `superuser`.
 */

/** The only roles this server recognises. */
export const KNOWN_ROLES: readonly string[] = ["viewer", "operator", "admin"];

/** What a principal gets when filtering leaves nothing. */
export const DEFAULT_ROLE = "viewer";

const KNOWN = new Set(KNOWN_ROLES);

/** What the reference iterates for a given claim. */
function claimedRoles(roles: unknown): Iterable<unknown> {
  // A bare string iterates as characters, none of which is a role — matching
  // the reference, where the same happens for the same reason.
  if (typeof roles === "string") {
    return roles;
  }
  if (typeof roles === "object" && roles !== null) {
    // A mapping iterates its keys, an array or set its values.
    return Symbol.iterator in (roles as object) ? (roles as Iterable<unknown>) : Object.keys(roles);
  }
  // Anything else is not iterable, and the reference raises rather than
  // guessing. Returning the default role here would hide the caller's bug and
  // grant access on a type error.
  throw new TypeError("roles is not iterable");
}

/**
 * Filter arbitrary claimed roles down to the ones that exist.
 *
 * Falls back to the *least* privileged role rather than an empty set, which
 * a caller could read as "no restrictions".
 *
 * @throws {TypeError} When the claim is not something roles can be read from.
 */
export function filterKnownRoles(roles: unknown): ReadonlySet<string> {
  const allowed = new Set<string>();
  for (const role of claimedRoles(roles)) {
    const cleaned = String(role).trim().toLowerCase();
    if (cleaned !== "" && KNOWN.has(cleaned)) {
      allowed.add(cleaned);
    }
  }
  return allowed.size > 0 ? allowed : new Set([DEFAULT_ROLE]);
}
