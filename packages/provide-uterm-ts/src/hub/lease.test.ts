//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { FakeLeaseHub, FakeWorkerSocket, session } from "../testing/lease-harness.ts";
import {
  DASHBOARD_LEASE_MAX_SECONDS,
  DASHBOARD_LEASE_MIN_SECONDS,
  HijackLeaseManager,
  type HijackSession,
  WorkerRegistry,
  WorkerTermState,
} from "./index.ts";

interface HubLeaseGolden {
  now: number;
  dashboard_lease_s: number;
  clamps: Array<{ name: string; requested: number; clamped: number; via_setter: number }>;
  compute_expirations: Array<{
    name: string;
    owner: string | null;
    owner_expires_at: number | null;
    session_expires_at: number | null;
    browser_expired: boolean;
    rest_expired: boolean;
    owner_after: string | null;
    session_after: number | null;
  }>;
  acquire_rest: Array<{
    name: string;
    ok: boolean;
    reason: string | null;
    session_hijack_id: string | null;
    session_expires_at: number | null;
    pending_after: string | null;
    worker_ws_cleared: boolean;
    pause_sent: boolean;
  }>;
  acquire_ws: Array<{
    name: string;
    ok: boolean;
    reason: string | null;
    owner_is_browser: boolean;
    owner_expires_at: number | null;
  }>;
  touch: Record<string, number | null>;
  release: {
    unknown_worker: [boolean, boolean];
    [key: string]: unknown;
  };
}

const golden = loadGolden<HubLeaseGolden>("hub_lease_golden.json");
const NOW = golden.now;

/** A browser stand-in, compared by identity as the hub compares WebSockets. */
const BROWSER = { id: "browser" };
const OTHER = { id: "other" };

/** How a recorded case describes the worker it needs. */
interface WorkerShape {
  connected?: boolean;
  inputMode?: "hijack" | "open";
  owner?: object;
  ownerExpiresAt?: number;
  session?: HijackSession;
  pending?: string;
  failSend?: boolean;
}

/** A manager over a fresh registry, plus the pieces the assertions read. */
function build(shape?: WorkerShape, dashboardLeaseSeconds = golden.dashboard_lease_s) {
  const registry = new WorkerRegistry<WorkerTermState>();
  const hub = new FakeLeaseHub();
  hub.now = () => NOW;
  const manager = new HijackLeaseManager({
    registry,
    hub,
    dashboardLeaseSeconds,
    now: () => NOW,
    wallNow: () => NOW,
  });
  let socket: FakeWorkerSocket | undefined;
  if (shape !== undefined) {
    const state = new WorkerTermState({ now: () => NOW });
    if (shape.connected !== false) {
      socket = new FakeWorkerSocket(shape.failSend ?? false);
      state.workerWs = socket;
    }
    state.inputMode = shape.inputMode ?? "hijack";
    state.hijackOwner = shape.owner;
    state.hijackOwnerExpiresAt = shape.ownerExpiresAt;
    state.hijackSession = shape.session;
    state.hijackPending = shape.pending;
    registry.put("w1", state);
  }
  return { manager, registry, hub, socket };
}

/** The worker shape each recorded acquire case sets up. */
const REST_SHAPES: Record<string, WorkerShape | undefined> = {
  "unknown worker": undefined,
  "worker not connected": { connected: false },
  "open input mode": { inputMode: "open" },
  "dashboard hijack held": { owner: OTHER, ownerExpiresAt: NOW + 10 },
  "rest lease held": { session: session(NOW + 10) },
  "another acquire reserving": { pending: "other" },
  "expired dashboard lease does not block": { owner: OTHER, ownerExpiresAt: NOW - 10 },
  "expired rest lease does not block": { session: session(NOW - 10) },
  "pause send fails": { failSend: true },
  granted: {},
};

const WS_SHAPES: Record<string, WorkerShape | undefined> = {
  "unknown worker": undefined,
  "worker not connected": { connected: false },
  "dashboard hijack held": { owner: OTHER, ownerExpiresAt: NOW + 10 },
  "rest lease held": { session: session(NOW + 10) },
  "another acquire reserving": { pending: "other" },
  "open input mode is not a guard here": { inputMode: "open" },
  granted: {},
};

describe("dashboard lease clamp", () => {
  it.each(golden.clamps)("$name", (record) => {
    const { manager } = build({}, record.requested);
    expect(manager.dashboardLeaseSeconds).toBe(record.clamped);
    manager.dashboardLeaseSeconds = record.requested;
    expect(manager.dashboardLeaseSeconds).toBe(record.via_setter);
  });

  it("exposes the reference bounds", () => {
    expect(DASHBOARD_LEASE_MIN_SECONDS).toBe(golden.clamps.find((c) => c.name === "at the floor")?.clamped);
    expect(DASHBOARD_LEASE_MAX_SECONDS).toBe(golden.clamps.find((c) => c.name === "at the ceiling")?.clamped);
  });

  it("truncates a fractional TTL toward zero", () => {
    const { manager } = build({}, 30.9);
    expect(manager.dashboardLeaseSeconds).toBe(30);
  });
});

describe("default clocks", () => {
  /** A manager on the real clocks, which every other test replaces. */
  function onRealClocks() {
    const registry = new WorkerRegistry<WorkerTermState>();
    const hub = new FakeLeaseHub();
    hub.now = () => performance.now() / 1000;
    const manager = new HijackLeaseManager({ registry, hub, dashboardLeaseSeconds: golden.dashboard_lease_s });
    const state = new WorkerTermState();
    state.workerWs = new FakeWorkerSocket();
    registry.put("w1", state);
    return { manager, registry, state };
  }

  it("measures a granted dashboard lease from monotonic time", async () => {
    const { manager, state } = onRealClocks();
    await manager.tryAcquireWs("w1", BROWSER);
    const expected = performance.now() / 1000 + golden.dashboard_lease_s;
    expect(state.hijackOwnerExpiresAt).toBeDefined();
    expect(Math.abs((state.hijackOwnerExpiresAt ?? 0) - expected)).toBeLessThan(1);
  });

  it("stamps a pause frame with wall time", async () => {
    // The worker shows this to a human, so it has to be a real timestamp
    // rather than the monotonic reading the lease arithmetic uses.
    const { manager, state } = onRealClocks();
    await manager.tryAcquireRest("w1", { owner: "cli", leaseSeconds: 90, hijackId: "new", now: 0 });
    const frame = (state.workerWs as FakeWorkerSocket).sent[0] ?? "";
    const ts = Number(/"ts":([0-9.]+)/.exec(frame)?.[1] ?? 0);
    expect(Math.abs(ts - Date.now() / 1000)).toBeLessThan(5);
  });
});

describe("computeLeaseExpirations", () => {
  it.each(golden.compute_expirations)("$name", (record) => {
    const state = new WorkerTermState({ now: () => NOW });
    state.hijackOwner = record.owner === null ? undefined : BROWSER;
    state.hijackOwnerExpiresAt = record.owner_expires_at ?? undefined;
    state.hijackSession = record.session_expires_at === null ? undefined : session(record.session_expires_at);

    expect(HijackLeaseManager.computeLeaseExpirations(state, NOW)).toStrictEqual({
      browserExpired: record.browser_expired,
      restExpired: record.rest_expired,
    });
    // This is a read: it reports what *would* expire without clearing it, so
    // the caller can decide whether to act.
    expect(state.hijackOwner === undefined).toBe(record.owner_after === null);
    expect(state.hijackSession?.leaseExpiresAt).toBe(record.session_after ?? undefined);
  });
});

describe("HijackLeaseManager.tryAcquireRest", () => {
  it.each(golden.acquire_rest)("$name", async (record) => {
    // The refusal reason is surfaced by the API, so a port that returned the
    // right boolean with the wrong reason would pass a smoke test and be
    // wrong in production. Each guard is driven in the reference's order.
    const { manager, registry, socket } = build(REST_SHAPES[record.name]);
    const result = await manager.tryAcquireRest("w1", {
      owner: "cli",
      leaseSeconds: 90,
      hijackId: "new",
      now: NOW,
    });

    expect(result.ok).toBe(record.ok);
    expect(result.reason).toBe(record.reason ?? undefined);

    const after = registry.get("w1");
    expect(after?.hijackSession?.hijackId).toBe(record.session_hijack_id ?? undefined);
    expect(after?.hijackSession?.leaseExpiresAt).toBe(record.session_expires_at ?? undefined);
    expect(after?.hijackPending).toBe(record.pending_after ?? undefined);
    expect((socket?.sent.length ?? 0) > 0).toBe(record.pause_sent);
  });

  it("clears the worker socket when the pause cannot be delivered", () => {
    // A worker that cannot be paused is gone, and leaving the dead socket in
    // place would let the next acquire believe there is still a worker there.
    const record = golden.acquire_rest.find((entry) => entry.name === "pause send fails");
    expect(record?.worker_ws_cleared).toBe(true);
    expect(record?.reason).toBe("no_worker");
  });

  it("clears its reservation when the pause fails", async () => {
    const { manager, registry } = build({ failSend: true });
    await manager.tryAcquireRest("w1", { owner: "cli", leaseSeconds: 90, hijackId: "new", now: NOW });
    const after = registry.get("w1");
    expect(after?.hijackPending).toBeUndefined();
    expect(after?.workerWs).toBeUndefined();
  });

  it("sends a pause frame naming the acquiring owner", async () => {
    const { manager, socket } = build({});
    await manager.tryAcquireRest("w1", { owner: "cli", leaseSeconds: 90, hijackId: "new", now: NOW });
    expect(socket?.sent).toHaveLength(1);
    const frame = socket?.sent[0] ?? "";
    expect(frame.startsWith("\x10\x02")).toBe(true);
    expect(frame).toContain('"action":"pause"');
    expect(frame).toContain('"owner":"cli"');
    expect(frame).toContain('"hijack_id":"new"');
  });

  it("does not clobber a reservation that superseded its own", async () => {
    // The reservation is what makes the lock-free pause safe. If another
    // acquire took the slot while this one was writing, this one must lose
    // rather than overwrite the winner's lease.
    const { manager, registry } = build({});
    const state = registry.require("w1");
    const socket = state.workerWs as FakeWorkerSocket;
    const original = socket.sendText.bind(socket);
    (state.workerWs as FakeWorkerSocket).sendText = async (payload: string) => {
      // Mid-pause, a competing acquire replaces the reservation.
      state.hijackPending = "someone-else";
      await original(payload);
    };

    const result = await manager.tryAcquireRest("w1", {
      owner: "cli",
      leaseSeconds: 90,
      hijackId: "new",
      now: NOW,
    });
    expect(result).toStrictEqual({ ok: false, reason: "no_worker" });
    expect(state.hijackSession).toBeUndefined();
    // The rollback must leave the *other* reservation alone.
    expect(state.hijackPending).toBe("someone-else");
  });

  it("leaves a replaced socket alone when the pause fails", async () => {
    // The socket is only cleared when it is still the one that failed. A
    // reconnect that landed mid-pause installs a live socket, and clearing
    // that would disconnect a worker that is perfectly healthy.
    const { manager, registry } = build({ failSend: true });
    const state = registry.require("w1");
    const failing = state.workerWs as FakeWorkerSocket;
    const replacement = new FakeWorkerSocket();
    failing.sendText = async () => {
      state.workerWs = replacement;
      throw new Error("socket closed");
    };
    const result = await manager.tryAcquireRest("w1", {
      owner: "cli",
      leaseSeconds: 90,
      hijackId: "new",
      now: NOW,
    });
    expect(result).toStrictEqual({ ok: false, reason: "no_worker" });
    expect(state.workerWs).toBe(replacement);
  });

  it("refuses when the worker vanishes mid-pause", async () => {
    const { manager, registry } = build({});
    const state = registry.require("w1");
    const socket = state.workerWs as FakeWorkerSocket;
    const original = socket.sendText.bind(socket);
    socket.sendText = async (payload: string) => {
      registry.discard("w1");
      await original(payload);
    };
    const result = await manager.tryAcquireRest("w1", {
      owner: "cli",
      leaseSeconds: 90,
      hijackId: "new",
      now: NOW,
    });
    expect(result).toStrictEqual({ ok: false, reason: "no_worker" });
  });
});

describe("HijackLeaseManager.tryAcquireWs", () => {
  it.each(golden.acquire_ws)("$name", async (record) => {
    const { manager, registry } = build(WS_SHAPES[record.name]);
    const result = await manager.tryAcquireWs("w1", BROWSER);
    expect(result.ok).toBe(record.ok);
    expect(result.reason).toBe(record.reason ?? undefined);

    const after = registry.get("w1");
    expect(after?.hijackOwner === BROWSER).toBe(record.owner_is_browser);
    expect(after?.hijackOwnerExpiresAt).toBe(record.owner_expires_at ?? undefined);
  });

  it("is refused while a REST acquire holds the reservation", () => {
    // The pause window is exactly when dual ownership would be possible, so
    // an outstanding reservation counts as taken.
    const record = golden.acquire_ws.find((entry) => entry.name === "another acquire reserving");
    expect(record?.ok).toBe(false);
    expect(record?.reason).toBe("already_hijacked");
  });

  it("grants regardless of input mode", () => {
    // Open mode blocks a REST acquire but not a dashboard one; the guards
    // genuinely differ between the two paths.
    const record = golden.acquire_ws.find((entry) => entry.name === "open input mode is not a guard here");
    expect(record?.ok).toBe(true);
    expect(golden.acquire_rest.find((entry) => entry.name === "open input mode")?.reason).toBe("open_mode");
  });
});

describe("HijackLeaseManager.touchOwner", () => {
  it("extends by the configured TTL by default", async () => {
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.touchOwner("w1")).toBe(golden.touch.default_ttl);
  });

  it("extends by an explicit TTL", async () => {
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.touchOwner("w1", 120)).toBe(golden.touch.explicit);
  });

  it("clamps an explicit TTL to the bounds", async () => {
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.touchOwner("w1", 9999)).toBe(golden.touch.clamped_high);
    expect(await manager.touchOwner("w1", 0)).toBe(golden.touch.clamped_low);
  });

  it("returns nothing for an unknown worker or one with no owner", async () => {
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.touchOwner("nope")).toBe(golden.touch.unknown ?? undefined);
    const bare = build({});
    expect(await bare.manager.touchOwner("w1")).toBe(golden.touch.no_owner ?? undefined);
  });
});

describe("HijackLeaseManager.touchIfOwner", () => {
  it("extends the lease for the holder", async () => {
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.touchIfOwner("w1", BROWSER)).toBe(golden.touch.matching);
  });

  it("refuses for anyone else", async () => {
    // Otherwise any connected browser could keep someone else's hold alive.
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.touchIfOwner("w1", OTHER)).toBe(golden.touch.mismatched ?? undefined);
  });

  it("extends a perpetual hold", async () => {
    const { manager } = build({ owner: BROWSER });
    expect(await manager.touchIfOwner("w1", BROWSER)).toBe(golden.touch.perpetual_touch);
  });

  it("returns nothing for an unknown worker", async () => {
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.touchIfOwner("nope", BROWSER)).toBeUndefined();
  });
});

describe("HijackLeaseManager.tryReleaseWs", () => {
  /** Assert one recorded release case. */
  async function check(key: string, shape: WorkerShape, ws: object): Promise<void> {
    const expected = golden.release[key] as { ok: boolean; rest_active: boolean; owner_cleared: boolean };
    const { manager, registry } = build(shape);
    const result = await manager.tryReleaseWs("w1", ws);
    expect(result).toStrictEqual({ ok: expected.ok, restActive: expected.rest_active });
    expect(registry.require("w1").hijackOwner === undefined).toBe(expected.owner_cleared);
  }

  it("releases for the holder", async () => {
    await check("owner_matches", { owner: BROWSER, ownerExpiresAt: NOW + 10 }, BROWSER);
  });

  it("refuses for anyone else and leaves the hold intact", async () => {
    await check("owner_mismatch", { owner: OTHER, ownerExpiresAt: NOW + 10 }, BROWSER);
  });

  it("reports a live REST lease when refusing", async () => {
    // The caller uses this to decide whether the worker can resume; a
    // refused release with a REST lease still held must not resume it.
    await check(
      "owner_mismatch_with_rest",
      { owner: OTHER, ownerExpiresAt: NOW + 10, session: session(NOW + 10) },
      BROWSER,
    );
  });

  it("reports a live REST lease when releasing", async () => {
    await check(
      "released_with_rest_live",
      { owner: BROWSER, ownerExpiresAt: NOW + 10, session: session(NOW + 10) },
      BROWSER,
    );
  });

  it("refuses once the lease has expired", async () => {
    await check("lease_expired", { owner: BROWSER, ownerExpiresAt: NOW - 10 }, BROWSER);
  });

  it("refuses for an unknown worker", async () => {
    const { manager } = build();
    const [ok, restActive] = golden.release.unknown_worker;
    expect(await manager.tryReleaseWs("nope", BROWSER)).toStrictEqual({ ok, restActive });
  });
});
