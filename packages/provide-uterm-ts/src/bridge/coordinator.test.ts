//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { COORDINATOR_LEASE_MAX_SECONDS, COORDINATOR_LEASE_MIN_SECONDS, HijackCoordinator } from "./index.ts";

interface Outcome {
  ok: boolean;
  error: string | null;
  is_renewal: boolean;
  has_session: boolean;
  owner: string | null;
  expires_at: number | null;
}

interface BridgeGolden {
  now: number;
  coordinator: Record<string, Outcome | boolean>;
  lease_clamps: Array<{ name: string; requested: number; granted_seconds: number }>;
}

const golden = loadGolden<BridgeGolden>("bridge_golden.json");
const NOW = golden.now;

/** The recorded outcome under `key`. */
function recorded(key: string): Outcome {
  return golden.coordinator[key] as Outcome;
}

/** Assert a result against its recorded outcome. */
function expectOutcome(result: ReturnType<HijackCoordinator["acquire"]>, key: string): void {
  const expected = recorded(key);
  expect(result.ok).toBe(expected.ok);
  expect(result.error).toBe(expected.error ?? undefined);
  expect(result.isRenewal).toBe(expected.is_renewal);
  expect(result.session !== undefined).toBe(expected.has_session);
  expect(result.session?.owner).toBe(expected.owner ?? undefined);
  expect(result.session?.leaseExpiresAt).toBe(expected.expires_at ?? undefined);
}

describe("HijackCoordinator.acquire", () => {
  it("grants an idle session", () => {
    const coordinator = new HijackCoordinator();
    expectOutcome(coordinator.acquire("alice", 90, NOW), "first");
  });

  it("renews for the same owner", () => {
    const coordinator = new HijackCoordinator();
    coordinator.acquire("alice", 90, NOW);
    expectOutcome(coordinator.acquire("alice", 90, NOW + 10), "renewal");
  });

  it("mints a fresh id on renewal", () => {
    // The caller is handed an authoritative token for the new period, so an
    // id captured before the renewal stops working — which is what makes a
    // leaked one time-bounded rather than permanent.
    expect(golden.coordinator["renewal_mints_new_id"]).toBe(true);
    const coordinator = new HijackCoordinator();
    const first = coordinator.acquire("alice", 90, NOW);
    const renewed = coordinator.acquire("alice", 90, NOW + 10);
    expect(renewed.session?.hijackId).not.toBe(first.session?.hijackId);
  });

  it("refuses a different owner while the lease is live", () => {
    const coordinator = new HijackCoordinator();
    coordinator.acquire("alice", 90, NOW);
    coordinator.acquire("alice", 90, NOW + 10);
    expectOutcome(coordinator.acquire("bob", 90, NOW + 20), "contested");
  });

  it("hands the session to whoever asks once the lease lapses", () => {
    // Time-bounded expiry is the whole safety property: an operator who
    // walks away cannot hold the session forever.
    const coordinator = new HijackCoordinator();
    coordinator.acquire("alice", 1, NOW);
    expectOutcome(coordinator.acquire("bob", 90, NOW + 2), "after_expiry");
  });

  it.each(golden.lease_clamps)("clamps a lease of $requested ($name)", (record) => {
    const coordinator = new HijackCoordinator();
    const result = coordinator.acquire("alice", record.requested, NOW);
    expect((result.session?.leaseExpiresAt ?? 0) - NOW).toBe(record.granted_seconds);
  });

  it("exposes its own bounds, which are not the hub's", () => {
    // The coordinator caps at an hour; the hub's dashboard leases cap at ten
    // minutes and its REST leases at four hours. Three different limits for
    // three different exposures.
    expect(COORDINATOR_LEASE_MIN_SECONDS).toBe(1);
    expect(COORDINATOR_LEASE_MAX_SECONDS).toBe(3600);
    expect(golden.lease_clamps.find((entry) => entry.name === "at the ceiling")?.granted_seconds).toBe(3600);
  });
});

describe("HijackCoordinator.heartbeat", () => {
  /** A coordinator holding alice's lease, with its id. */
  function held() {
    const coordinator = new HijackCoordinator();
    const result = coordinator.acquire("alice", 90, NOW);
    return { coordinator, hijackId: result.session?.hijackId ?? "" };
  }

  it("extends the lease", () => {
    const { coordinator, hijackId } = held();
    expectOutcome(coordinator.heartbeat(hijackId, 120, { now: NOW + 5 }), "good_beat");
  });

  it("extends for the matching owner", () => {
    const { coordinator, hijackId } = held();
    coordinator.heartbeat(hijackId, 120, { now: NOW + 5 });
    expectOutcome(coordinator.heartbeat(hijackId, 120, { owner: "alice", now: NOW + 8 }), "right_owner");
  });

  it("refuses a mismatched id", () => {
    // Knowing that a session is held is not knowing the token for it.
    const { coordinator, hijackId } = held();
    coordinator.heartbeat(hijackId, 120, { now: NOW + 5 });
    expectOutcome(coordinator.heartbeat("not-the-id", 120, { now: NOW + 6 }), "wrong_id");
  });

  it("refuses a mismatched owner when one is given", () => {
    // Defence in depth over the id: a leaked token still cannot be renewed
    // by someone claiming to be a different operator.
    const { coordinator, hijackId } = held();
    coordinator.heartbeat(hijackId, 120, { now: NOW + 5 });
    expectOutcome(coordinator.heartbeat(hijackId, 120, { owner: "bob", now: NOW + 7 }), "wrong_owner");
  });

  it("does not check the owner when none is given", () => {
    const { coordinator, hijackId } = held();
    expect(coordinator.heartbeat(hijackId, 120, { now: NOW + 5 }).ok).toBe(true);
  });

  it("refuses when nothing is held", () => {
    expectOutcome(new HijackCoordinator().heartbeat("anything", 90, { now: NOW }), "beat_when_idle");
  });

  it("refuses once the lease has lapsed", () => {
    // The expiry sweep runs on read, so a heartbeat arriving late finds
    // nothing rather than resurrecting a dead lease.
    const { coordinator, hijackId } = held();
    expect(coordinator.heartbeat(hijackId, 90, { now: NOW + 1000 }).error).toBe("not_hijacked");
  });

  it("clamps the extension", () => {
    const { coordinator, hijackId } = held();
    const beat = coordinator.heartbeat(hijackId, 99_999, { now: NOW });
    expect((beat.session?.leaseExpiresAt ?? 0) - NOW).toBe(COORDINATOR_LEASE_MAX_SECONDS);
  });

  it("reads its own clock when given no time", () => {
    // The `now` argument is a testing seam; production callers omit it, and
    // omitting it must still measure against real monotonic time.
    const coordinator = new HijackCoordinator();
    const acquired = coordinator.acquire("alice", 90);
    const beat = coordinator.heartbeat(acquired.session?.hijackId ?? "", 120);
    expect(beat.ok).toBe(true);
    expect((beat.session?.leaseExpiresAt ?? 0) - performance.now() / 1000).toBeGreaterThan(119);
  });

  it("records when it was last renewed", () => {
    const { coordinator, hijackId } = held();
    const beat = coordinator.heartbeat(hijackId, 90, { now: NOW + 5 });
    expect(beat.session?.lastHeartbeat).toBe(NOW + 5);
  });
});

describe("HijackCoordinator.release", () => {
  it("clears the lease for its holder", () => {
    const coordinator = new HijackCoordinator();
    const result = coordinator.acquire("alice", 90, NOW);
    expectOutcome(coordinator.release(result.session?.hijackId ?? ""), "good_release");
  });

  it("refuses a mismatched id and leaves the lease alone", () => {
    const coordinator = new HijackCoordinator();
    coordinator.acquire("alice", 90, NOW);
    expectOutcome(coordinator.release("not-the-id"), "wrong_release");
  });

  it("refuses when nothing is held", () => {
    expectOutcome(new HijackCoordinator().release("anything"), "release_when_idle");
  });

  it("refuses a second release", () => {
    const coordinator = new HijackCoordinator();
    const result = coordinator.acquire("alice", 90, NOW);
    coordinator.release(result.session?.hijackId ?? "");
    expectOutcome(coordinator.release(result.session?.hijackId ?? ""), "double_release");
  });

  it("still clears a lease that has already lapsed", () => {
    // Release deliberately does not consult expiry: whoever holds the id can
    // always clean up, even if they noticed late. The clock has to actually
    // be past the expiry for this to mean anything.
    expect(recorded("stale_release").ok).toBe(true);
    const clock = { now: NOW };
    const coordinator = new HijackCoordinator({ now: () => clock.now });
    const result = coordinator.acquire("alice", 1, NOW);
    clock.now = NOW + 1000;
    expect(coordinator.release(result.session?.hijackId ?? "").ok).toBe(true);
  });

  it("finds nothing once the lapsed lease has been swept", () => {
    // Reading `session` is not free: it clears a lease it finds expired. So
    // whether a late release succeeds depends on whether anything looked
    // first — faithful to the reference, and worth stating because it makes
    // an innocuous-looking read change the outcome.
    const clock = { now: NOW };
    const coordinator = new HijackCoordinator({ now: () => clock.now });
    const result = coordinator.acquire("alice", 1, NOW);
    clock.now = NOW + 1000;
    expect(coordinator.session).toBeUndefined();
    expect(coordinator.release(result.session?.hijackId ?? "").error).toBe("not_hijacked");
  });
});

describe("HijackCoordinator.canSendInput", () => {
  /** A coordinator on a clock the test moves. */
  function gated() {
    const clock = { now: NOW };
    const coordinator = new HijackCoordinator({ now: () => clock.now });
    const result = coordinator.acquire("alice", 90, NOW);
    return { coordinator, clock, hijackId: result.session?.hijackId ?? "" };
  }

  it("allows the holder", () => {
    const { coordinator, clock, hijackId } = gated();
    clock.now = NOW + 1;
    expect(coordinator.canSendInput(hijackId)).toBe(golden.coordinator["can_send_with_id"]);
  });

  it("refuses a mismatched id", () => {
    const { coordinator, clock } = gated();
    clock.now = NOW + 1;
    expect(coordinator.canSendInput("nope")).toBe(golden.coordinator["can_send_wrong_id"]);
  });

  it("refuses a caller with no id", () => {
    const { coordinator, clock } = gated();
    clock.now = NOW + 1;
    expect(coordinator.canSendInput()).toBe(golden.coordinator["can_send_no_id"]);
  });

  it("refuses when nothing is held", () => {
    expect(new HijackCoordinator().canSendInput("anything")).toBe(golden.coordinator["can_send_when_idle"]);
  });

  it("refuses once the lease has lapsed", () => {
    // The gate closes on time, not on release: an operator who stops
    // heartbeating stops being able to type.
    const { coordinator, clock, hijackId } = gated();
    clock.now = NOW + 1000;
    expect(coordinator.canSendInput(hijackId)).toBe(golden.coordinator["can_send_after_expiry"]);
  });

  it("reads its own clock by default", () => {
    const coordinator = new HijackCoordinator();
    coordinator.acquire("alice", 90);
    expect(coordinator.canSendInput(coordinator.session?.hijackId)).toBe(true);
  });
});

describe("HijackCoordinator.session", () => {
  it("reports the live lease", () => {
    const clock = { now: NOW };
    const coordinator = new HijackCoordinator({ now: () => clock.now });
    coordinator.acquire("alice", 90, NOW);
    expect(coordinator.session?.owner).toBe("alice");
    clock.now = NOW + 1000;
    expect(coordinator.session).toBeUndefined();
  });

  it("reports nothing when idle", () => {
    expect(new HijackCoordinator().session).toBeUndefined();
  });

  it("treats a lease expiring exactly now as gone", () => {
    // The comparison is `<=`, so the last instant of a lease is already
    // outside it — a caller cannot squeeze a keystroke through on the tick
    // their lease runs out.
    const clock = { now: NOW };
    const coordinator = new HijackCoordinator({ now: () => clock.now });
    const result = coordinator.acquire("alice", 10, NOW);
    clock.now = NOW + 9.999;
    expect(coordinator.session).toBeDefined();
    clock.now = NOW + 10;
    expect(coordinator.session).toBeUndefined();
    expect(coordinator.canSendInput(result.session?.hijackId)).toBe(false);
  });

  it("clears a lapsed lease as it reads it", () => {
    // The sweep happens on read, so a later acquire does not have to know
    // that the previous holder ever existed.
    const clock = { now: NOW };
    const coordinator = new HijackCoordinator({ now: () => clock.now });
    coordinator.acquire("alice", 1, NOW);
    clock.now = NOW + 5;
    expect(coordinator.session).toBeUndefined();
    expect(coordinator.acquire("bob", 90, NOW + 5).ok).toBe(true);
  });
});
