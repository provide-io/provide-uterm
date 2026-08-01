//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  APPROVAL_PRUNE_TTL,
  APPROVAL_STATUSES,
  type ApprovalRequestInput,
  type ApprovalStatus,
  InMemoryApprovalStore,
  type StoredApprovalRequest,
} from "./index.ts";

interface ApprovalsGolden {
  statuses: string[];
  cleanup: {
    now: number;
    prune_ttl: number;
    outcomes: Array<{
      name: string;
      initial_status: string;
      expires_at: number;
      present: boolean;
      status: string | null;
    }>;
    notified: string[];
  };
  claim: {
    first_claim: boolean;
    second_claim: boolean;
    unknown_claim: boolean;
    status_after_double_claim: string;
    status_after_resolve: string;
    unknown_get_is_none: boolean;
  };
  replacement: { status: string; expires_at: number };
}

const golden = loadGolden<ApprovalsGolden>("approvals_golden.json");
const NOW = golden.cleanup.now;

/** A request whose only interesting fields are its status and its expiry. */
function request(id: string, status: ApprovalStatus, expiresAt: number): ApprovalRequestInput {
  return {
    id,
    workerId: "w1",
    submitterId: "s1",
    command: "ls",
    status,
    createdAt: NOW - 120,
    expiresAt,
  };
}

describe("approval statuses", () => {
  it("matches the reference lifecycle states", () => {
    expect([...APPROVAL_STATUSES]).toStrictEqual(golden.statuses);
  });

  it("prunes on the reference TTL", () => {
    expect(APPROVAL_PRUNE_TTL).toBe(golden.cleanup.prune_ttl);
  });
});

describe("InMemoryApprovalStore storage", () => {
  it("returns undefined for an id it does not hold", () => {
    expect(golden.claim.unknown_get_is_none).toBe(true);
    expect(new InMemoryApprovalStore().get("nope")).toBeUndefined();
  });

  it("assigns an opaque revision to a stored copy", () => {
    const store = new InMemoryApprovalStore();
    const req = request("r0", "pending", NOW + 60);
    const stored = store.add(req);
    expect(stored?.revision).toBe(1);
    expect(store.get("r0")).toStrictEqual(stored);
    expect(store.get("r0")).not.toBe(stored);
    expect(req).not.toHaveProperty("revision");
  });

  it("rejects a live duplicate id without replacing its revision", () => {
    const store = new InMemoryApprovalStore();
    const first = store.add(request("r0", "pending", NOW + 60));
    const duplicate = store.add(request("r0", "approved", NOW + 120));
    const req = store.get("r0");
    expect(duplicate).toBeUndefined();
    expect(req).toStrictEqual(first);
    expect(req?.status).toBe(golden.replacement.status);
    expect(req?.expiresAt).toBe(golden.replacement.expires_at);
  });

  it("assigns a fresh revision when a pruned id is reused", async () => {
    let now = NOW;
    const store = new InMemoryApprovalStore({ now: () => now });
    const first = store.add(request("r0", "approved", NOW - APPROVAL_PRUNE_TTL - 1));
    now += 1;
    await store.cleanupExpired();

    const second = store.add(request("r0", "pending", NOW + 60));

    expect(first?.revision).toBe(1);
    expect(second?.revision).toBe(2);
  });

  it("throws before the revision counter can exceed safe integer precision", () => {
    const store = new InMemoryApprovalStore({ initialRevision: Number.MAX_SAFE_INTEGER });

    expect(() => store.add(request("r0", "pending", NOW + 60))).toThrow("approval revision space exhausted");
    expect(store.get("r0")).toBeUndefined();
  });

  it.each([-1, Number.MAX_VALUE])("rejects invalid initial revision %s", (initialRevision) => {
    expect(() => new InMemoryApprovalStore({ initialRevision })).toThrow("initial approval revision");
  });
});

describe("InMemoryApprovalStore claim", () => {
  it("transitions a pending request exactly once", () => {
    // The whole point of claim over resolve: only the winner injects the
    // held command, so a concurrent approve and reject cannot both run it.
    const store = new InMemoryApprovalStore();
    const stored = store.add(request("r0", "pending", NOW + 60));
    expect(store.claim("r0", "approved", stored?.revision ?? 0)).toBe(golden.claim.first_claim);
    expect(store.claim("r0", "rejected", stored?.revision ?? 0)).toBe(golden.claim.second_claim);
    expect(store.get("r0")?.status).toBe(golden.claim.status_after_double_claim);
  });

  it("refuses to claim an unknown request", () => {
    expect(new InMemoryApprovalStore().claim("nope", "approved", 1)).toBe(golden.claim.unknown_claim);
  });

  it("times out an expired request on the spot but notifies from cleanup", async () => {
    // A late decision cannot inject, and the reference never runs listener
    // code inside a decision call — the snapshot waits for the next cleanup
    // pass, and is delivered exactly once.
    let now = NOW;
    const store = new InMemoryApprovalStore({ now: () => now });
    const stored = store.add(request("r0", "pending", NOW + 60));
    now = NOW + 60;
    const notified: string[] = [];
    store.onExpired = (approval) => {
      notified.push(`${approval.id}#${approval.revision}`);
    };

    expect(store.claim("r0", "approved", stored?.revision ?? 0)).toBe(false);
    expect(store.get("r0")?.status).toBe("timeout");
    expect(notified).toStrictEqual([]);

    await store.cleanupExpired();
    expect(notified).toStrictEqual([`r0#${stored?.revision}`]);
    await store.cleanupExpired();
    expect(notified).toStrictEqual([`r0#${stored?.revision}`]);
  });

  it("refuses to claim a request that is already terminal", () => {
    const store = new InMemoryApprovalStore();
    const stored = store.add(request("r0", "timeout", NOW + 60));
    expect(store.claim("r0", "approved", stored?.revision ?? 0)).toBe(false);
    expect(store.get("r0")?.status).toBe("timeout");
  });

  it("refuses a stale revision after an id is reused", async () => {
    let now = NOW;
    const store = new InMemoryApprovalStore({ now: () => now });
    const first = store.add(request("r0", "approved", NOW - APPROVAL_PRUNE_TTL - 1));
    now += 1;
    await store.cleanupExpired();
    const second = store.add(request("r0", "pending", NOW + 60));

    expect(store.claim("r0", "approved", first?.revision ?? 0)).toBe(false);
    store.resolve("r0", "rejected", first?.revision ?? 0);
    expect(store.get("r0")?.status).toBe("pending");
    expect(store.claim("r0", "approved", second?.revision ?? 0)).toBe(true);
  });
});

describe("InMemoryApprovalStore resolve", () => {
  it("transitions a pending request", () => {
    // The corpus records this against the wall clock, under which the fixture
    // expiry has long passed — so what it pins is the expiry path.
    const store = new InMemoryApprovalStore();
    const stored = store.add(request("r1", "pending", NOW + 60));
    store.resolve("r1", "rejected", stored?.revision ?? 0);
    expect(store.get("r1")?.status).toBe(golden.claim.status_after_resolve);
  });

  it("resolves a pending request inside its window", () => {
    const store = new InMemoryApprovalStore({ now: () => NOW });
    const stored = store.add(request("r1", "pending", NOW + 60));
    store.resolve("r1", "rejected", stored?.revision ?? 0);
    expect(store.get("r1")?.status).toBe("rejected");
  });

  it("leaves an already-resolved request alone", () => {
    const store = new InMemoryApprovalStore();
    const stored = store.add(request("r0", "approved", NOW + 60));
    store.resolve("r0", "rejected", stored?.revision ?? 0);
    expect(store.get("r0")?.status).toBe("approved");
  });

  it("does nothing for an unknown request rather than throwing", () => {
    const store = new InMemoryApprovalStore();
    expect(() => store.resolve("nope", "approved", 1)).not.toThrow();
    expect(store.get("nope")).toBeUndefined();
  });
});

describe("InMemoryApprovalStore cleanup", () => {
  /** Load every recorded case into one store and clean it once. */
  async function runCleanup(): Promise<{ store: InMemoryApprovalStore; notified: string[] }> {
    const store = new InMemoryApprovalStore({ now: () => NOW });
    const notified: string[] = [];
    store.onExpired = (approval) => {
      notified.push(approval.id);
    };
    golden.cleanup.outcomes.forEach((outcome, index) => {
      store.add(request(`r${index}`, outcome.initial_status as ApprovalStatus, outcome.expires_at));
    });
    await store.cleanupExpired();
    return { store, notified };
  }

  const cases = golden.cleanup.outcomes.map((outcome, index) => ({ ...outcome, index }));

  it.each(cases)("$name", async (outcome) => {
    // Both boundaries here are strict in the reference — a request expiring
    // exactly now is still pending — which is why they are recorded rather
    // than asserted from memory.
    const { store } = await runCleanup();
    const req = store.get(`r${outcome.index}`);
    expect(req !== undefined).toBe(outcome.present);
    expect(req?.status).toBe(outcome.status ?? undefined);
  });

  it("notifies only the requests that timed out", async () => {
    const { notified } = await runCleanup();
    expect(notified).toStrictEqual(golden.cleanup.notified);
  });

  it("notifies expiry with an immutable exact-revision snapshot", async () => {
    const store = new InMemoryApprovalStore({ now: () => NOW });
    const stored = store.add(request("expired", "pending", NOW - 1));
    const expired: StoredApprovalRequest[] = [];
    store.onExpired = (approval) => {
      expired.push(approval);
      approval.command = "mutated callback copy";
    };

    await store.cleanupExpired();

    expect(expired[0]?.revision).toBe(stored?.revision);
    expect(store.get("expired")?.command).toBe("ls");
  });

  it("does not notify when no callback is set", async () => {
    const store = new InMemoryApprovalStore({ now: () => NOW });
    store.add(request("r0", "pending", NOW - 60));
    await expect(store.cleanupExpired()).resolves.toBeUndefined();
    expect(store.get("r0")?.status).toBe("timeout");
  });

  it("awaits each async callback in turn", async () => {
    const store = new InMemoryApprovalStore({ now: () => NOW });
    store.add(request("r0", "pending", NOW - 60));
    store.add(request("r1", "pending", NOW - 30));
    const events: string[] = [];
    store.onExpired = async (approval) => {
      events.push(`start ${approval.id}`);
      // A timer, not a resolved promise: awaiting the returned promise once
      // would drain a microtask on its own, so a microtask-length callback
      // cannot tell a real await from a dropped one.
      await new Promise((resolve) => setTimeout(resolve, 0));
      events.push(`end ${approval.id}`);
    };
    await store.cleanupExpired();
    // Cleanup must not return before subscribers have pruned their own state,
    // and must not overlap two callbacks — hence end r0 before start r1.
    expect(events).toStrictEqual(["start r0", "end r0", "start r1", "end r1"]);
  });

  it("finishes every mutation before the first callback runs", async () => {
    // The callback is user code and may be slow; the store must not be
    // half-updated while it runs, or a subscriber reading back sees a
    // request that is still pending and a prune that has not happened.
    const store = new InMemoryApprovalStore({ now: () => NOW });
    store.add(request("r0", "pending", NOW - 60));
    store.add(request("r1", "pending", NOW - 30));
    store.add(request("r2", "approved", NOW - APPROVAL_PRUNE_TTL - 60));
    const observed: Array<string | undefined> = [];
    store.onExpired = (approval) => {
      observed.push(approval.id, store.get("r0")?.status, store.get("r1")?.status, store.get("r2")?.status);
    };
    await store.cleanupExpired();
    expect(observed).toStrictEqual(["r0", "timeout", "timeout", undefined, "r1", "timeout", "timeout", undefined]);
  });

  it("defaults to the wall clock", async () => {
    // The default clock is real time, so a request that expired in 1970 is
    // expired under it and one expiring far in the future is not.
    const store = new InMemoryApprovalStore();
    store.add(request("past", "pending", 0));
    store.add(request("future", "pending", Date.now() / 1000 + 3600));
    await store.cleanupExpired();
    expect(store.get("past")?.status).toBe("timeout");
    expect(store.get("future")?.status).toBe("pending");
  });
});
