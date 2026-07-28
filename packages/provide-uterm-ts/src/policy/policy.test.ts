//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadSpec } from "../testing/golden.ts";
import {
  canInject,
  canPerform,
  ERR_INSUFFICIENT_ROLE,
  ERR_NO_LEASE,
  ERR_SESSION_INACTIVE,
  ROLE_RANK,
  roleRank,
} from "./index.ts";

interface BehaviorVectors {
  version: string;
  policy_cases: Array<{
    op: string;
    role: string;
    lease_owned: boolean;
    session_active: boolean;
    allowed: boolean;
    error: string | null;
  }>;
}

// Not this port's own corpus: Python, Go and C# are held to the same file, so
// a divergence here is a divergence between implementations.
const spec = loadSpec<BehaviorVectors>("behavior_vectors.json");

describe("the shared policy vectors", () => {
  it.each(spec.policy_cases)("$op as $role lease=$lease_owned session=$session_active", (vector) => {
    const result = canPerform(vector.op, {
      role: vector.role,
      leaseOwned: vector.lease_owned,
      sessionActive: vector.session_active,
    });
    expect(result ?? null).toBe(vector.error);
    expect(result === undefined).toBe(vector.allowed);
  });

  it("covers every operation and role the contract names", () => {
    // A vector file that lost a case would make this suite pass by testing
    // less, so the shape it must have is asserted rather than assumed.
    expect(spec.policy_cases.length).toBeGreaterThanOrEqual(48);
    expect([...new Set(spec.policy_cases.map((vector) => vector.op))].sort()).toStrictEqual([
      "hijack_acquire",
      "hijack_release",
      "hijack_step",
      "input_inject",
    ]);
    expect([...new Set(spec.policy_cases.map((vector) => vector.role))].sort()).toStrictEqual([
      "admin",
      "operator",
      "viewer",
    ]);
  });
});

describe("roleRank", () => {
  it("orders the roles", () => {
    expect(roleRank("viewer")).toBeLessThan(roleRank("operator"));
    expect(roleRank("operator")).toBeLessThan(roleRank("admin"));
  });

  it("puts an unknown role below every known one", () => {
    // Below viewer, not equal to it: a typo in a role name must not quietly
    // grant the lowest real role's privileges.
    expect(roleRank("wizard")).toBeLessThan(roleRank("viewer"));
    expect(roleRank("")).toBeLessThan(roleRank("viewer"));
  });

  it("matches the shared ranks", () => {
    expect(ROLE_RANK).toStrictEqual({ viewer: 0, operator: 1, admin: 2 });
  });
});

describe("canPerform", () => {
  it("refuses an operation it does not know", () => {
    // Denying by default is what keeps a new operation from shipping
    // ungated: adding one to a caller before adding it here fails closed.
    expect(canPerform("format_disk", { role: "admin", leaseOwned: true, sessionActive: true })).toBe(
      "forbidden: unknown operation format_disk",
    );
  });

  it("names the operation it refused", () => {
    expect(canPerform("nonsense", { role: "admin", leaseOwned: true })).toContain("nonsense");
  });

  it("checks the role before the lease", () => {
    // A viewer with no lease is refused for being a viewer. Reporting the
    // lease instead would send them looking for one they still could not use.
    expect(canPerform("input_inject", { role: "viewer", leaseOwned: false })).toBe(ERR_INSUFFICIENT_ROLE);
  });

  it("checks the lease before the session", () => {
    expect(canPerform("hijack_step", { role: "operator", leaseOwned: false, sessionActive: false })).toBe(ERR_NO_LEASE);
  });

  it("lets a higher role through", () => {
    expect(canPerform("input_inject", { role: "admin", leaseOwned: true })).toBeUndefined();
  });

  it("does not let an unknown role through", () => {
    expect(canPerform("input_inject", { role: "root", leaseOwned: true })).toBe(ERR_INSUFFICIENT_ROLE);
  });

  it("requires a lease only for the operations that change the terminal", () => {
    // Releasing a lease you do not hold is a no-op, not an escalation, and
    // acquiring one obviously cannot require already having it.
    expect(canPerform("hijack_release", { role: "operator", leaseOwned: false })).toBeUndefined();
    expect(canPerform("hijack_acquire", { role: "operator", leaseOwned: false })).toBeUndefined();
  });

  it("requires an active session only where one is needed", () => {
    // Injecting into a session that has gone is harmless; stepping it, or
    // acquiring a lease on it, is a request that can never be satisfied.
    expect(canPerform("input_inject", { role: "operator", leaseOwned: true, sessionActive: false })).toBeUndefined();
    expect(canPerform("hijack_release", { role: "operator", leaseOwned: true, sessionActive: false })).toBeUndefined();
    expect(canPerform("hijack_acquire", { role: "operator", leaseOwned: false, sessionActive: false })).toBe(
      ERR_SESSION_INACTIVE,
    );
    expect(canPerform("hijack_step", { role: "operator", leaseOwned: true, sessionActive: false })).toBe(
      ERR_SESSION_INACTIVE,
    );
  });

  it("treats the session as active when the caller does not say", () => {
    // The default is the permissive one, so a caller that never learned about
    // the flag is not silently refused.
    expect(canPerform("hijack_acquire", { role: "operator", leaseOwned: false })).toBeUndefined();
  });

  it("uses the error strings the other ports use", () => {
    // These cross the wire to clients written against Go and C#; changing one
    // is changing an API.
    expect(ERR_INSUFFICIENT_ROLE).toBe("forbidden: insufficient role");
    expect(ERR_NO_LEASE).toBe("forbidden: no active lease");
    expect(ERR_SESSION_INACTIVE).toBe("forbidden: session inactive");
  });
});

describe("canInject", () => {
  it("allows an operator holding a lease", () => {
    expect(canInject("s1", "lease-1", "operator")).toBeUndefined();
  });

  it("refuses an operator with no lease", () => {
    // An empty lease id is no lease, not a lease named "".
    expect(canInject("s1", "", "operator")).toBe(ERR_NO_LEASE);
  });

  it("refuses a viewer even with a lease", () => {
    expect(canInject("s1", "lease-1", "viewer")).toBe(ERR_INSUFFICIENT_ROLE);
  });

  it("does not gate on the session", () => {
    // The session id is reserved for audit correlation. Gating on it would
    // make injection depend on something the caller never passes.
    expect(canInject("", "lease-1", "operator")).toBeUndefined();
    expect(canInject("any-session-at-all", "lease-1", "operator")).toBeUndefined();
  });

  it("agrees with canPerform for the same inputs", () => {
    for (const role of ["viewer", "operator", "admin", "unknown"]) {
      for (const lease of ["", "lease-1"]) {
        expect(canInject("s1", lease, role)).toBe(canPerform("input_inject", { role, leaseOwned: lease !== "" }));
      }
    }
  });
});
