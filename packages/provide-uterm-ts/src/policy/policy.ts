//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Pure role, lease and session gates.
 *
 * Port of the Python module `provide.uterm.bridge.policy` and the Go package
 * `policy`. All four implementations are held to the same vectors in
 * `spec/behavior_vectors.json`, and the error strings cross the wire to
 * clients written against any of them — changing one is changing an API.
 */

/** How the roles order. Anything absent ranks below all of them. */
export const ROLE_RANK: Readonly<Record<string, number>> = {
  viewer: 0,
  operator: 1,
  admin: 2,
};

/** The caller's role is too low for the operation. */
export const ERR_INSUFFICIENT_ROLE = "forbidden: insufficient role";

/** The operation needs a hijack lease and the caller holds none. */
export const ERR_NO_LEASE = "forbidden: no active lease";

/** The operation needs a live session and there is not one. */
export const ERR_SESSION_INACTIVE = "forbidden: session inactive";

/** The lowest role each operation accepts. */
const OP_MIN_ROLE: Readonly<Record<string, string>> = {
  input_inject: "operator",
  hijack_step: "operator",
  hijack_release: "operator",
  hijack_acquire: "operator",
};

/** Operations that change the terminal, and so need a lease. */
const OP_NEEDS_LEASE = new Set(["input_inject", "hijack_step"]);

/** Operations that cannot be satisfied without a live session. */
const OP_NEEDS_SESSION = new Set(["hijack_step", "hijack_acquire"]);

/**
 * The rank of `role`, or below every known role when it is not one.
 *
 * Below viewer rather than equal to it: a typo in a role name must not
 * quietly grant the lowest real role's privileges.
 */
export function roleRank(role: string): number {
  return Object.hasOwn(ROLE_RANK, role) ? (ROLE_RANK[role] as number) : -1;
}

/** What a caller is asking to do. */
export interface PolicyRequest {
  /** The caller's role. */
  role: string;
  /** Whether the caller holds the hijack lease. */
  leaseOwned: boolean;
  /** Whether the session is live. Defaults to true. */
  sessionActive?: boolean;
}

/**
 * Whether `op` is allowed, or why not.
 *
 * The checks run role, then lease, then session, so the answer names the
 * first thing the caller would have to change — reporting a missing lease to
 * a viewer would send them looking for one they still could not use.
 *
 * @returns Nothing when allowed, or a stable forbidden string.
 */
export function canPerform(op: string, options: PolicyRequest): string | undefined {
  const minRole = OP_MIN_ROLE[op];
  // Denying by default is what keeps a new operation from shipping ungated:
  // adding one to a caller before adding it here fails closed.
  if (minRole === undefined) {
    return `forbidden: unknown operation ${op}`;
  }
  if (roleRank(options.role) < (ROLE_RANK[minRole] as number)) {
    return ERR_INSUFFICIENT_ROLE;
  }
  if (OP_NEEDS_LEASE.has(op) && !options.leaseOwned) {
    return ERR_NO_LEASE;
  }
  if (OP_NEEDS_SESSION.has(op) && options.sessionActive === false) {
    return ERR_SESSION_INACTIVE;
  }
  return undefined;
}

/**
 * The RFB and human-relay entry point.
 *
 * `sessionId` is reserved for audit correlation and deliberately does not
 * gate: injection does not require a live session, so making it depend on one
 * would refuse a request the other ports allow.
 */
export function canInject(sessionId: string, leaseId: string, principalRole: string): string | undefined {
  void sessionId;
  // An empty lease id is no lease, not a lease named "".
  return canPerform("input_inject", { role: principalRole, leaseOwned: leaseId !== "" });
}
