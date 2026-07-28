//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The shared authorization contract.
 *
 * Port of the Python module `provide.uterm.bridge.policy`, mirroring the Go
 * package `policy` and the matrices in `spec/behavior.json`. Every port has
 * to answer this identically, so it is a pure function of the caller's
 * standing with no I/O and no clock.
 *
 * The refusal *strings* are as much of the contract as the verdicts: callers
 * match on them to decide what to tell the operator.
 */

/**
 * Rank of each known role.
 *
 * An operation names a minimum, and anything at or above it is allowed —
 * which is what makes admin a superset of operator without enumerating it.
 */
export const ROLE_RANK: Readonly<Record<string, number>> = {
  viewer: 0,
  operator: 1,
  admin: 2,
};

/** Refusal for a role that cannot perform the operation. */
export const ERR_INSUFFICIENT_ROLE = "forbidden: insufficient role";

/** Refusal for an operation needing a lease the caller does not hold. */
export const ERR_NO_LEASE = "forbidden: no active lease";

/** Refusal for an operation needing a live session. */
export const ERR_SESSION_INACTIVE = "forbidden: session inactive";

/** The lowest role each operation accepts. */
const MIN_ROLE: Readonly<Record<string, string>> = {
  input_inject: "operator",
  hijack_step: "operator",
  hijack_release: "operator",
  hijack_acquire: "operator",
};

/** Operations that require the caller to hold the lease. */
const NEEDS_LEASE = new Set(["input_inject", "hijack_step"]);

/**
 * Operations that require a live session.
 *
 * Injecting is deliberately absent: a session can be mid-reconnect and still
 * owe its holder the keystrokes they already sent.
 */
const NEEDS_SESSION = new Set(["hijack_step", "hijack_acquire"]);

/** The caller's standing for an authorization check. */
export interface PolicyContext {
  /** The role the caller holds. */
  role: string;
  /** Whether the caller holds the session's lease. */
  leaseOwned: boolean;
  /** Whether the session is live. Assumed live when omitted. */
  sessionActive?: boolean;
}

/**
 * Rank for a role, below every known one when unrecognised.
 *
 * `-1` rather than `0`: an unrecognised role must not silently be treated as
 * a viewer, which is itself a permission level.
 */
export function roleRank(role: string): number {
  return ROLE_RANK[role] ?? -1;
}

/**
 * Nothing when the operation is allowed, otherwise why it is refused.
 *
 * The order of the checks is part of the contract. An unknown operation is
 * refused first, so a typo cannot be allowed for whoever happens to satisfy
 * the rest; then role, then lease, then session — so the caller is told about
 * the thing they can actually act on.
 */
export function canPerform(op: string, context: PolicyContext): string | undefined {
  const minRole = MIN_ROLE[op];
  if (minRole === undefined) {
    return `forbidden: unknown operation ${op}`;
  }
  if (roleRank(context.role) < (ROLE_RANK[minRole] as number)) {
    return ERR_INSUFFICIENT_ROLE;
  }
  if (NEEDS_LEASE.has(op) && !context.leaseOwned) {
    return ERR_NO_LEASE;
  }
  if (NEEDS_SESSION.has(op) && context.sessionActive === false) {
    return ERR_SESSION_INACTIVE;
  }
  return undefined;
}

/**
 * Whether input may be injected into a session.
 *
 * `sessionId` is reserved for audit correlation and deliberately does not
 * reach the decision — gating on it would make the answer depend on a value
 * the caller chooses. An empty lease id means no lease is held.
 */
export function canInject(sessionId: string, leaseId: string, principalRole: string): string | undefined {
  void sessionId;
  return canPerform("input_inject", { role: principalRole, leaseOwned: leaseId !== "" });
}
