//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type ApprovalRecord,
  bootstrapControlPlane,
  CONTROL_PLANE_DEFAULTS,
  ControlPlaneConflictError,
  DEFAULT_CAPABILITIES,
  type LeaseRecord,
  MemoryControlPlane,
  type ResumeTokenRecord,
  type SessionRecord,
} from "./index.ts";

interface ControlPlaneGolden {
  conflict: { loser_error: string; winner: string; second_is_closed: boolean };
  disjoint: Record<string, string>;
  isolation: Record<string, unknown>;
  reap: { removed: number; cutoff: number; survivors: Record<string, string[]> };
  audit_head: Record<string, unknown>;
  stores: Record<string, unknown>;
  bootstrap: { memory_builds: string; unknown_backend: string };
  capabilities: Record<string, boolean>;
  default_config: { backend: string; database_url: string };
}

const golden = loadGolden<ControlPlaneGolden>("controlplane_golden.json");
const NOW = 1000;

/** A lease record with the fields a test cares about. */
function lease(sessionId: string, owner: string, expiresAt = NOW + 60, deletedAt?: number): LeaseRecord {
  return {
    sessionId,
    hijackId: `h-${owner}`,
    owner,
    leaseExpiresAt: expiresAt,
    createdAt: NOW,
    ...(deletedAt === undefined ? {} : { deletedAt }),
  };
}

/** A session record with the fields a test cares about. */
function session(sessionId: string, state: SessionRecord["lifecycleState"] = "running", deletedAt?: number) {
  return {
    sessionId,
    displayName: sessionId,
    connectorType: "shell",
    owner: "alice",
    visibility: "operator",
    lifecycleState: state,
    createdAt: 1,
    updatedAt: 1,
    ...(deletedAt === undefined ? {} : { deletedAt }),
  } satisfies SessionRecord;
}

/** An approval record with the fields a test cares about. */
function approval(
  approvalId: string,
  state: ApprovalRecord["state"] = "pending",
  createdAt = 1,
  resolvedAt?: number,
): ApprovalRecord {
  return {
    approvalId,
    sessionId: "s",
    command: "rm -rf /",
    requestedBy: "alice",
    state,
    createdAt,
    ...(resolvedAt === undefined ? {} : { resolvedAt }),
  };
}

/** A resume token with the fields a test cares about. */
function resumeToken(tokenValue: string, expiresAt: number, revokedAt?: number): ResumeTokenRecord {
  return {
    tokenValue,
    sessionId: "s",
    role: "viewer",
    createdAt: 1,
    expiresAt,
    ...(revokedAt === undefined ? {} : { revokedAt }),
  };
}

describe("two transactions racing for the same key", () => {
  it("lets exactly one win", async () => {
    // The whole reason the memory backend detects conflicts: a lease-acquire
    // race must yield one winner here as it does on SQLite, or a deployment
    // that develops against memory finds out in production.
    const plane = new MemoryControlPlane();
    const first = await plane.begin();
    const second = await plane.begin();
    await plane.leaseStore(first).putLease(lease("s1", "alice"));
    await plane.leaseStore(second).putLease(lease("s1", "bob"));
    await first.commit();
    await expect(second.commit()).rejects.toThrow(golden.conflict.loser_error);

    const settled = await plane.begin();
    expect((await plane.leaseStore(settled).getLease("s1"))?.owner).toBe(golden.conflict.winner);
  });

  it("closes the transaction that lost", async () => {
    // A caller that retried on the same transaction would apply its writes
    // on top of the winner's.
    const plane = new MemoryControlPlane();
    const first = await plane.begin();
    const second = await plane.begin();
    await plane.leaseStore(first).putLease(lease("s1", "alice"));
    await plane.leaseStore(second).putLease(lease("s1", "bob"));
    await first.commit();
    await expect(second.commit()).rejects.toThrow(ControlPlaneConflictError);
    expect(second.closed).toBe(golden.conflict.second_is_closed);
  });

  it("leaves nothing behind when it refuses", async () => {
    // The conflict is detected across every table before anything is merged,
    // so a transaction that touched two tables does not half-commit.
    const plane = new MemoryControlPlane();
    const first = await plane.begin();
    const second = await plane.begin();
    await plane.leaseStore(first).putLease(lease("s1", "alice"));
    await plane.leaseStore(second).putLease(lease("s1", "bob"));
    await plane.sessionStore(second).upsertSession(session("s2"));
    await first.commit();
    await expect(second.commit()).rejects.toThrow(ControlPlaneConflictError);

    const check = await plane.begin();
    expect(await plane.sessionStore(check).getSession("s2")).toBeUndefined();
  });
});

describe("two transactions touching different keys", () => {
  it("lets both through", async () => {
    // Merging whole tables would have the later commit silently undo the
    // earlier one.
    const plane = new MemoryControlPlane();
    const first = await plane.begin();
    const second = await plane.begin();
    await plane.leaseStore(first).putLease(lease("s1", "alice"));
    await plane.leaseStore(second).putLease(lease("s2", "bob"));
    await first.commit();
    await second.commit();

    const check = await plane.begin();
    const store = plane.leaseStore(check);
    expect((await store.getLease("s1"))?.owner).toBe(golden.disjoint.s1);
    expect((await store.getLease("s2"))?.owner).toBe(golden.disjoint.s2);
  });

  it("lets a delete through beside an unrelated write", async () => {
    const plane = new MemoryControlPlane();
    const setup = await plane.begin();
    await plane.leaseStore(setup).putLease(lease("s1", "alice"));
    await setup.commit();

    const remover = await plane.begin();
    const writer = await plane.begin();
    await plane.leaseStore(remover).clearLease("s1");
    await plane.leaseStore(writer).putLease(lease("s2", "bob"));
    await remover.commit();
    await writer.commit();

    const check = await plane.begin();
    expect(await plane.leaseStore(check).getLease("s1")).toBeUndefined();
    expect(await plane.leaseStore(check).getLease("s2")).toBeDefined();
  });
});

describe("isolation", () => {
  it("hides an uncommitted write from everybody else", async () => {
    const plane = new MemoryControlPlane();
    const writer = await plane.begin();
    await plane.leaseStore(writer).putLease(lease("s1", "alice"));
    const reader = await plane.begin();
    expect(await plane.leaseStore(reader).getLease("s1")).toBeUndefined();
    expect(golden.isolation.another_transaction_cannot_see_it).toBe(true);
  });

  it("shows a transaction its own writes", async () => {
    const plane = new MemoryControlPlane();
    const writer = await plane.begin();
    await plane.leaseStore(writer).putLease(lease("s1", "alice"));
    expect((await plane.leaseStore(writer).getLease("s1"))?.owner).toBe(
      golden.isolation.its_own_writes_are_visible_to_it,
    );
  });

  it("keeps a transaction on the state it started with", async () => {
    // A reader that saw a later commit halfway through would see two
    // different worlds in one unit of work.
    const plane = new MemoryControlPlane();
    const writer = await plane.begin();
    await plane.leaseStore(writer).putLease(lease("s1", "alice"));
    const reader = await plane.begin();
    await writer.commit();
    expect(await plane.leaseStore(reader).getLease("s1")).toBeUndefined();

    const fresh = await plane.begin();
    expect((await plane.leaseStore(fresh).getLease("s1"))?.owner).toBe(golden.isolation.a_new_transaction_can);
  });

  it("throws away a rolled-back write", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    await plane.leaseStore(tx).putLease(lease("s2", "bob"));
    await tx.rollback();
    const check = await plane.begin();
    expect(await plane.leaseStore(check).getLease("s2")).toBeUndefined();
  });

  it("treats a second commit as nothing to do", async () => {
    // A caller that commits in a finally block should not have to track
    // whether it already did.
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    await tx.commit();
    await expect(tx.commit()).resolves.toBeUndefined();
    expect(tx.closed).toBe(golden.isolation.committing_twice_is_a_no_op);
  });

  it("does not apply a rolled-back transaction on a later commit", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    await plane.leaseStore(tx).putLease(lease("s1", "alice"));
    await tx.rollback();
    await tx.commit();
    const check = await plane.begin();
    expect(await plane.leaseStore(check).getLease("s1")).toBeUndefined();
  });
});

describe("reaping", () => {
  /** A plane holding one of everything the reaper looks at. */
  async function populated() {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const leases = plane.leaseStore(tx);
    const tokens = plane.tokenStore(tx);
    const sessions = plane.sessionStore(tx);
    const approvals = plane.approvalStore(tx);

    await leases.putLease(lease("expired", "a", 899));
    await leases.putLease(lease("on-the-cutoff", "b", 900));
    await leases.putLease(lease("live", "c", NOW + 60));
    await leases.putLease(lease("soft-deleted", "d", NOW + 60, 899));
    await tokens.createResumeToken(resumeToken("old", 899));
    await tokens.createResumeToken(resumeToken("fresh", NOW + 60));
    await tokens.createResumeToken(resumeToken("revoked", NOW + 60, 899));
    await tokens.putSessionToken({
      sessionId: "s",
      tokenKind: "never-expires",
      tokenValue: "t",
      createdAt: 1,
      expiresAt: undefined,
    });
    await sessions.upsertSession(session("gone", "deleted", 899));
    await sessions.upsertSession(session("here"));
    await approvals.putApproval(approval("settled", "approved", 1, 899));
    await approvals.putApproval(approval("waiting"));
    await tx.commit();
    return plane;
  }

  it("removes exactly what the reference removes", async () => {
    const plane = await populated();
    expect(await plane.reap({ now: NOW, retentionS: 100 })).toBe(golden.reap.removed);

    const after = await plane.begin();
    expect([...after.state.leases.keys()].sort()).toStrictEqual(golden.reap.survivors.leases);
    expect([...after.state.resumeTokens.keys()].sort()).toStrictEqual(golden.reap.survivors.resume_tokens);
    expect([...after.state.sessions.keys()].sort()).toStrictEqual(golden.reap.survivors.sessions);
    expect([...after.state.approvals.keys()].sort()).toStrictEqual(golden.reap.survivors.approvals);
    expect(after.state.sessionTokens.size).toBe((golden.reap.survivors.session_tokens ?? []).length);
  });

  it("keeps a row exactly on the cutoff", async () => {
    // Strict `<`, matching SQLite's predicate: an inclusive comparison would
    // prune a row the durable backend keeps.
    expect(golden.reap.survivors.leases).toContain("on-the-cutoff");
    expect(golden.reap.survivors.leases).not.toContain("expired");
  });

  it("never prunes a row whose timestamp is absent", async () => {
    // A token with no expiry does not expire, and a session that was never
    // deleted is not old.
    expect(golden.reap.survivors.session_tokens).toStrictEqual(["s:never-expires"]);
    expect(golden.reap.survivors.sessions).toContain("here");
  });

  it("prunes on either timestamp a row carries", async () => {
    // A lease can go because it expired *or* because it was soft-deleted.
    expect(golden.reap.survivors.leases).not.toContain("soft-deleted");
    expect(golden.reap.survivors.resume_tokens).not.toContain("revoked");
  });

  it("leaves an unresolved approval alone", async () => {
    expect(golden.reap.survivors.approvals).toStrictEqual(["waiting"]);
  });

  it("removes nothing from an empty plane", async () => {
    expect(await new MemoryControlPlane().reap({ now: NOW, retentionS: 100 })).toBe(0);
  });
});

describe("the audit head", () => {
  it("starts with none", async () => {
    expect(await new MemoryControlPlane().getAuditHead()).toBeUndefined();
    expect(golden.audit_head.starts_empty).toBe(true);
  });

  it("only ever moves forward", async () => {
    // Accepting a lower sequence would let a replayed older head pass as an
    // update, which is exactly the rollback the chain exists to detect.
    const plane = new MemoryControlPlane();
    await plane.setAuditHead(5, "hash-5");
    expect(await plane.getAuditHead()).toStrictEqual(golden.audit_head.after_first);
    await plane.setAuditHead(4, "hash-4");
    expect(await plane.getAuditHead()).toStrictEqual(golden.audit_head.a_lower_sequence_is_ignored);
    await plane.setAuditHead(5, "hash-5-again");
    expect(await plane.getAuditHead()).toStrictEqual(golden.audit_head.an_equal_sequence_is_ignored);
    await plane.setAuditHead(6, "hash-6");
    expect(await plane.getAuditHead()).toStrictEqual(golden.audit_head.a_higher_sequence_moves_it);
  });

  it("ignores an equal sequence with a different hash", async () => {
    // Two records claiming the same position is a fork, not an update.
    expect(golden.audit_head.an_equal_sequence_is_ignored).toStrictEqual([5, "hash-5"]);
  });

  it("is not part of a transaction", async () => {
    // It is set outside one and is non-durable; a transaction that copied it
    // would roll it back on abort.
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    await plane.setAuditHead(1, "h");
    await tx.rollback();
    expect(await plane.getAuditHead()).toStrictEqual([1, "h"]);
  });
});

describe("the stores", () => {
  it("lists pending approvals oldest first, then by id", async () => {
    // Matching the SQLite ORDER BY, so a queue consumer sees the same order
    // whichever backend it is on.
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const approvals = plane.approvalStore(tx);
    await approvals.putApproval(approval("c", "pending", 2));
    await approvals.putApproval(approval("a", "pending", 2));
    await approvals.putApproval(approval("b", "pending", 1));
    await approvals.putApproval(approval("done", "approved", 0.5, 1));
    expect((await approvals.listPending()).map((record) => record.approvalId)).toStrictEqual(
      golden.stores.pending_in_order,
    );
  });

  it("keeps a resolved approval readable but not pending", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const approvals = plane.approvalStore(tx);
    await approvals.putApproval(approval("done", "approved", 0.5, 1));
    expect(await approvals.getApproval("done")).toBeDefined();
    expect(await approvals.listPending()).toStrictEqual([]);
  });

  it("reads a revoked resume token as absent", async () => {
    // Every caller is asking "may this token resume", and a revoked one may
    // not — returning it would put the decision somewhere else.
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const tokens = plane.tokenStore(tx);
    await tokens.createResumeToken(resumeToken("dead", NOW + 60));
    await tokens.revokeResumeToken("dead", NOW);
    expect(await tokens.getResumeToken("dead")).toBeUndefined();
    expect(golden.stores.a_revoked_resume_token_reads_as_absent).toBe(true);
  });

  it("keeps a live resume token readable", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const tokens = plane.tokenStore(tx);
    await tokens.createResumeToken(resumeToken("live", NOW + 60));
    expect((await tokens.getResumeToken("live"))?.tokenValue).toBe(golden.stores.a_live_one_does_not);
  });

  it("does nothing when revoking a token it does not have", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    await expect(plane.tokenStore(tx).revokeResumeToken("never-existed", NOW)).resolves.toBeUndefined();
  });

  it("keys a session token by session and kind", async () => {
    // One session holds several tokens for different purposes; keying by
    // session alone would have them overwrite each other.
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const tokens = plane.tokenStore(tx);
    await tokens.putSessionToken({
      sessionId: "s",
      tokenKind: "join",
      tokenValue: "v1",
      createdAt: 1,
      expiresAt: undefined,
    });
    await tokens.putSessionToken({
      sessionId: "s",
      tokenKind: "watch",
      tokenValue: "w1",
      createdAt: 1,
      expiresAt: undefined,
    });
    await tokens.putSessionToken({
      sessionId: "s",
      tokenKind: "join",
      tokenValue: "v2",
      createdAt: 2,
      expiresAt: undefined,
    });
    expect((await tokens.getSessionToken("s", "join"))?.tokenValue).toBe(
      golden.stores.a_session_token_is_keyed_by_kind,
    );
    expect((await tokens.getSessionToken("s", "watch"))?.tokenValue).toBe("w1");
  });

  it("keeps a deleted session's row", async () => {
    // A session that vanished would take its own audit trail with it; the
    // reaper is what eventually removes it.
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const sessions = plane.sessionStore(tx);
    await sessions.upsertSession(session("s1"));
    await sessions.markDeleted("s1", NOW);
    const record = await sessions.getSession("s1");
    expect({ lifecycle_state: record?.lifecycleState, deleted_at: record?.deletedAt }).toStrictEqual(
      golden.stores.a_deleted_session_keeps_its_row,
    );
  });

  it("does nothing when deleting a session it does not have", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    await plane.sessionStore(tx).markDeleted("never-existed", NOW);
    expect(await plane.sessionStore(tx).getSession("never-existed")).toBeUndefined();
  });

  it("does nothing when clearing a lease it does not have", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    await expect(plane.leaseStore(tx).clearLease("never-existed")).resolves.toBeUndefined();
  });

  it("stores and drops a graphical target", async () => {
    const plane = new MemoryControlPlane();
    const tx = await plane.begin();
    const targets = plane.graphicalTargetStore(tx);
    await targets.putTarget({ targetId: "t1", kind: "vnc" });
    expect(await targets.getTarget("t1")).toStrictEqual({ targetId: "t1", kind: "vnc" });
    await targets.deleteTarget("t1");
    expect(await targets.getTarget("t1")).toBeUndefined();
  });
});

describe("the engine", () => {
  it("reports the recorded capabilities", () => {
    const plane = new MemoryControlPlane();
    expect({
      supports_transactions: plane.capabilities.supportsTransactions,
      supports_migrations: plane.capabilities.supportsMigrations,
      supports_retries: plane.capabilities.supportsRetries,
    }).toStrictEqual(golden.capabilities);
    expect(DEFAULT_CAPABILITIES.supportsTransactions).toBe(true);
  });

  it("takes capabilities from the caller", () => {
    const plane = new MemoryControlPlane({
      capabilities: { supportsTransactions: false, supportsMigrations: false, supportsRetries: false },
    });
    expect(plane.capabilities.supportsTransactions).toBe(false);
  });

  it("defaults to the in-memory backend", () => {
    const plane = new MemoryControlPlane();
    expect(plane.config.backend).toBe(golden.default_config.backend);
    expect(plane.config.databaseUrl).toBe(golden.default_config.database_url);
    expect(CONTROL_PLANE_DEFAULTS.backend).toBe("memory");
  });

  it("is what the factory builds for the memory backend", async () => {
    const plane = await bootstrapControlPlane({ backend: "memory" });
    expect(plane).toBeInstanceOf(MemoryControlPlane);
    expect(golden.bootstrap.memory_builds).toBe("MemoryControlPlane");
  });

  it("builds the default backend when asked for none", async () => {
    expect(await bootstrapControlPlane()).toBeInstanceOf(MemoryControlPlane);
  });

  it("refuses a backend nothing implements", async () => {
    // A deployment that asked for a durable store must not silently get a
    // volatile one.
    await expect(bootstrapControlPlane({ backend: "postgres" as "sqlite" })).rejects.toThrow(
      golden.bootstrap.unknown_backend,
    );
  });

  it("has nothing to open, close or migrate", async () => {
    // There is no schema and no connection; the lifecycle exists so the two
    // backends are interchangeable.
    const plane = new MemoryControlPlane();
    await expect(plane.open()).resolves.toBeUndefined();
    await expect(plane.migrate()).resolves.toBeUndefined();
    await expect(plane.close()).resolves.toBeUndefined();
  });
});
