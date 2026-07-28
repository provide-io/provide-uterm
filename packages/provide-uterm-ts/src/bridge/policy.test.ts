//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  canInject,
  canPerform,
  ERR_INSUFFICIENT_ROLE,
  ERR_NO_LEASE,
  ERR_SESSION_INACTIVE,
  ROLE_RANK,
  roleRank,
} from "./index.ts";

interface BridgeGolden {
  role_ranks: Record<string, number>;
  unknown_role_rank: number;
  policy: Array<{
    op: string;
    role: string;
    lease_owned: boolean;
    session_active: boolean;
    error: string | null;
  }>;
  inject: Array<{ role: string; lease_id: string; error: string | null; error_other_session: string | null }>;
}

const golden = loadGolden<BridgeGolden>("bridge_golden.json");

describe("roleRank", () => {
  it("matches the reference ranking", () => {
    expect(Object.fromEntries(Object.entries(ROLE_RANK))).toStrictEqual(golden.role_ranks);
  });

  it("ranks an unknown role below every known one", () => {
    // Not zero: an unrecognised role must not silently be treated as a
    // viewer, which is itself a permission level.
    expect(golden.unknown_role_rank).toBe(-1);
    expect(roleRank("nonsense")).toBe(-1);
    expect(roleRank("")).toBe(-1);
    expect(roleRank("nonsense")).toBeLessThan(roleRank("viewer"));
  });
});

describe("canPerform", () => {
  it.each(golden.policy)("$op / $role / lease=$lease_owned / session=$session_active", (record) => {
    // The whole matrix, enumerated rather than sampled: this is the shared
    // contract the Go port implements and spec/behavior.json documents, and a
    // port can agree on the common cases while diverging in a corner.
    expect(
      canPerform(record.op, {
        role: record.role,
        leaseOwned: record.lease_owned,
        sessionActive: record.session_active,
      }),
    ).toBe(record.error ?? undefined);
  });

  it("names an unknown operation in its refusal", () => {
    // Callers match on these strings, so the message is contract too.
    expect(canPerform("teleport", { role: "admin", leaseOwned: true })).toBe("forbidden: unknown operation teleport");
  });

  it("refuses an unknown operation before anything else", () => {
    // Otherwise a typo'd operation would be allowed for whoever happened to
    // satisfy the checks that follow.
    const record = golden.policy.find((entry) => entry.op === "unknown_op" && entry.role === "admin");
    expect(record?.error).toBe("forbidden: unknown operation unknown_op");
  });

  it("checks role before lease", () => {
    // A viewer with no lease is refused for their role, not their lease —
    // the caller is told the thing they can actually act on.
    expect(canPerform("input_inject", { role: "viewer", leaseOwned: false })).toBe(ERR_INSUFFICIENT_ROLE);
    expect(canPerform("input_inject", { role: "operator", leaseOwned: false })).toBe(ERR_NO_LEASE);
  });

  it("requires a lease only where the reference does", () => {
    // Releasing and acquiring do not need one; injecting and stepping do.
    expect(canPerform("hijack_release", { role: "operator", leaseOwned: false })).toBeUndefined();
    expect(canPerform("hijack_acquire", { role: "operator", leaseOwned: false })).toBeUndefined();
    expect(canPerform("input_inject", { role: "operator", leaseOwned: false })).toBe(ERR_NO_LEASE);
    expect(canPerform("hijack_step", { role: "operator", leaseOwned: false })).toBe(ERR_NO_LEASE);
  });

  it("requires an active session only where the reference does", () => {
    // Injecting deliberately does not: a session can be mid-reconnect and
    // still owe its holder the keystrokes they already sent.
    expect(canPerform("input_inject", { role: "operator", leaseOwned: true, sessionActive: false })).toBeUndefined();
    expect(canPerform("hijack_release", { role: "operator", leaseOwned: true, sessionActive: false })).toBeUndefined();
    expect(canPerform("hijack_step", { role: "operator", leaseOwned: true, sessionActive: false })).toBe(
      ERR_SESSION_INACTIVE,
    );
    expect(canPerform("hijack_acquire", { role: "operator", leaseOwned: true, sessionActive: false })).toBe(
      ERR_SESSION_INACTIVE,
    );
  });

  it("treats the session as active unless told otherwise", () => {
    expect(canPerform("hijack_step", { role: "operator", leaseOwned: true })).toBeUndefined();
  });

  it("lets an admin do everything an operator can", () => {
    for (const op of ["input_inject", "hijack_step", "hijack_release", "hijack_acquire"]) {
      expect(canPerform(op, { role: "admin", leaseOwned: true })).toBeUndefined();
    }
  });

  it("lets a viewer do nothing", () => {
    for (const op of ["input_inject", "hijack_step", "hijack_release", "hijack_acquire"]) {
      expect(canPerform(op, { role: "viewer", leaseOwned: true })).toBe(ERR_INSUFFICIENT_ROLE);
    }
  });
});

describe("canInject", () => {
  it.each(golden.inject)("role $role with lease '$lease_id'", (record) => {
    expect(canInject("session-1", record.lease_id, record.role)).toBe(record.error ?? undefined);
  });

  it("does not let the session id change the answer", () => {
    // It is reserved for audit correlation. Gating on it would make the
    // decision depend on a value the caller chooses.
    for (const record of golden.inject) {
      expect(record.error).toBe(record.error_other_session);
      expect(canInject("session-2", record.lease_id, record.role)).toBe(record.error ?? undefined);
    }
  });

  it("treats an empty lease id as holding no lease", () => {
    expect(canInject("s", "", "operator")).toBe(ERR_NO_LEASE);
    expect(canInject("s", "lease-1", "operator")).toBeUndefined();
  });
});
