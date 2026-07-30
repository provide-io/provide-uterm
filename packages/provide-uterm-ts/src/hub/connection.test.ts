//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type ConnectionHubCallbacks,
  ConnectionManager,
  ConnectionQuotaError,
  scanEventsForResume,
  WorkerCapacityError,
  WorkerRegistry,
  WorkerTermState,
} from "./index.ts";
import type { Connection, InputMode } from "./models.ts";

interface HubConnectionGolden {
  now: number;
  registers: Array<{
    name: string;
    has_state: boolean;
    rest_expiry: number | null;
    has_owner: boolean;
    prev_was_hijacked: boolean;
    session_cleared: boolean;
    session_after: number | null;
    owner_cleared: boolean;
    worker_attached: boolean;
  }>;
  worker_caps: Array<{
    name: string;
    existing: string[];
    cap: number;
    worker_id: string;
    admitted: boolean;
    reason: string | null;
    code: number | null;
    workers_after: string[];
  }>;
  principal_quotas: Array<{
    name: string;
    current: number;
    cap: number;
    admitted: boolean;
    reason: string | null;
    code: number | null;
    after: number;
  }>;
  quota_exemptions: Record<string, { counted: boolean; tracked: Record<string, number> }>;
  quota_rollback: {
    raised: boolean;
    count_after: number;
    principal_tracked: boolean;
    resume_token_tracked: boolean;
  };
  disconnects: Array<{
    name: string;
    is_owner: boolean;
    rest_expiry: number | null;
    owned_hijack: boolean;
    was_owner: boolean;
    rest_still_active: boolean;
    resume_without_owner: boolean;
    count_after: number;
    browsers_left: number;
  }>;
  resume_scans: Array<{ name: string; events: unknown[]; resume_needed: boolean }>;
  hellos: Array<{
    name: string;
    exists: boolean;
    mode: InputMode;
    rest_expiry: number | null;
    has_owner: boolean;
    version: number | null;
    applied: boolean;
    input_mode_after: string | null;
    protocol_version_after: number | null;
  }>;
  deregisters: Array<{
    name: string;
    same_socket: boolean;
    rest_expiry: number | null;
    has_owner: boolean;
    should_broadcast: boolean;
    was_hijacked: boolean;
    worker_cleared: boolean;
    session_cleared: boolean;
    owner_cleared: boolean;
  }>;
}

const golden = loadGolden<HubConnectionGolden>("hub_connection_golden.json");
const NOW = golden.now;

/** A socket identified only by name, optionally carrying a principal. */
function socket(name: string, subjectId?: unknown) {
  return {
    name,
    sendText: async () => {},
    ...(subjectId === undefined ? {} : { state: { utermPrincipal: { subjectId } } }),
  };
}

/** A recording stand-in for the hub surface the manager reaches through. */
class FakeHub implements ConnectionHubCallbacks {
  readonly registry = new WorkerRegistry<WorkerTermState>();
  readonly startupPendingBrowsers = new Set<Connection>();
  maxWorkers = 100;
  maxConnectionsPerPrincipal = 100;

  isHijacked(state: WorkerTermState): boolean {
    return this.isDashboardHijackActive(state) || this.hasValidRestLease(state);
  }

  isDashboardHijackActive(state: WorkerTermState): boolean {
    if (state.hijackOwner === undefined) {
      return false;
    }
    return state.hijackOwnerExpiresAt === undefined || state.hijackOwnerExpiresAt > NOW;
  }

  hasValidRestLease(state: WorkerTermState): boolean {
    return state.hijackSession !== undefined && state.hijackSession.leaseExpiresAt > NOW;
  }
}

/** A manager on a frozen clock. */
function build(configure?: (hub: FakeHub) => void) {
  const hub = new FakeHub();
  configure?.(hub);
  const manager = new ConnectionManager({ hub, now: () => NOW });
  return { hub, manager };
}

/** A worker state holding the leases a recorded case needs. */
function stateFor(restExpiry: number | null, hasOwner: boolean): WorkerTermState {
  const state = new WorkerTermState({ now: () => NOW });
  if (restExpiry !== null) {
    state.hijackSession = { hijackId: "h1", owner: "cli", leaseExpiresAt: restExpiry };
  }
  if (hasOwner) {
    state.hijackOwner = socket("old-browser");
    state.hijackOwnerExpiresAt = NOW + 60;
  }
  return state;
}

describe("ConnectionManager.registerWorker", () => {
  it.each(golden.registers)("$name", (record) => {
    // A worker socket dropping is routine — a Durable Object rotating, a
    // manager restarting, a blip — so a reconnect must not invalidate a lease
    // that has not actually expired.
    const { hub, manager } = build();
    if (record.has_state) {
      hub.registry.put("w1", stateFor(record.rest_expiry, record.has_owner));
    }
    const ws = socket("worker");
    expect(manager.registerWorker("w1", ws)).toBe(record.prev_was_hijacked);

    const state = hub.registry.require("w1");
    expect(state.hijackSession === undefined).toBe(record.session_cleared);
    expect(state.hijackSession?.leaseExpiresAt).toBe(record.session_after ?? undefined);
    expect(state.hijackOwner === undefined).toBe(record.owner_cleared);
    expect(state.workerWs).toBe(ws);
  });

  it("keeps a live REST lease across a reconnect", () => {
    // The scar this exists for: clearing it meant one blip invalidated the
    // holder's hijack id and every later send 404'd.
    const record = golden.registers.find((entry) => entry.name === "reconnect, live REST lease");
    expect(record?.prev_was_hijacked).toBe(false);
    expect(record?.session_cleared).toBe(false);
  });

  it("clears a lease that expired exactly now", () => {
    const record = golden.registers.find((entry) => entry.name === "reconnect, REST lease expiring exactly now");
    expect(record?.session_cleared).toBe(true);
  });
});

describe("ConnectionManager worker capacity", () => {
  it.each(golden.worker_caps)("$name", (record) => {
    const { hub, manager } = build((fake) => {
      fake.maxWorkers = record.cap;
    });
    for (const existing of record.existing) {
      hub.registry.put(existing, new WorkerTermState({ now: () => NOW }));
    }
    if (record.admitted) {
      expect(() => manager.registerWorker(record.worker_id, socket("worker"))).not.toThrow();
    } else {
      expect(() => manager.registerWorker(record.worker_id, socket("worker"))).toThrow(WorkerCapacityError);
    }
    expect(hub.registry.keys().sort()).toStrictEqual(record.workers_after);
  });

  it("always readmits a worker it already knows", () => {
    // Otherwise the cap turns a full hub into one that can never heal: the
    // workers already in it could never reconnect.
    const record = golden.worker_caps.find((entry) => entry.name === "reconnect at capacity");
    expect(record?.admitted).toBe(true);
  });

  it("refuses a new worker with the reference reason", () => {
    const record = golden.worker_caps.find((entry) => entry.name === "new worker at capacity");
    const { hub, manager } = build((fake) => {
      fake.maxWorkers = 2;
    });
    hub.registry.put("w1", new WorkerTermState({ now: () => NOW }));
    hub.registry.put("w2", new WorkerTermState({ now: () => NOW }));
    expect(() => manager.registerWorker("w3", socket("worker"))).toThrow(record?.reason ?? "");
  });
});

describe("ConnectionManager browser quota", () => {
  it.each(golden.principal_quotas)("$name", async (record) => {
    // The recorded cases seed a count that can exceed the cap — a limit
    // lowered while connections are already open, which the quota has to
    // handle by refusing rather than by going negative.
    const { hub, manager } = build((fake) => {
      fake.maxConnectionsPerPrincipal = Number.MAX_SAFE_INTEGER;
    });
    const alice = () => socket("browser", "alice");
    for (let index = 0; index < record.current; index += 1) {
      await manager.registerBrowser("w1", alice(), "viewer");
    }
    hub.maxConnectionsPerPrincipal = record.cap;
    const attempt = manager.registerBrowser("w1", alice(), "operator");
    if (record.admitted) {
      await expect(attempt).resolves.toBeDefined();
    } else {
      await expect(attempt).rejects.toThrow(record.reason ?? "");
    }
    expect(manager.principalConnectionCount("alice")).toBe(record.after);
  });

  it.each(Object.entries(golden.quota_exemptions))("counts %s correctly", async (label, outcome) => {
    // The consequence, not the bookkeeping: an exempt principal can open a
    // second connection at a cap of one, and a counted one cannot. Checking
    // a counter would pass even if the exemption were dropped entirely.
    const subjects: Record<string, unknown> = {
      "no principal": undefined,
      anonymous: "anonymous",
      "empty subject": "",
      "non-string subject": 7,
      "named subject": "alice",
    };
    const { manager } = build((fake) => {
      fake.maxConnectionsPerPrincipal = 1;
    });
    const connect = () => manager.registerBrowser("w1", socket("browser", subjects[label]), "viewer");
    await connect();
    if (outcome.counted) {
      await expect(connect()).rejects.toThrow(ConnectionQuotaError);
    } else {
      await expect(connect()).resolves.toBeDefined();
    }
  });

  it("rolls the count back when registration fails after the increment", async () => {
    // A leaked slot is unrecoverable: nothing reaps the counter, so the
    // principal is locked out at their limit until the process restarts.
    const expected = golden.quota_rollback;
    const { manager } = build((fake) => {
      fake.maxConnectionsPerPrincipal = 2;
    });
    const failing = new ConnectionManager({
      hub: (manager as unknown as { hub: FakeHub }).hub ?? new FakeHub(),
      now: () => NOW,
      createResumeToken: async () => {
        throw new Error("resume store unavailable");
      },
    });
    await expect(failing.registerBrowser("w1", socket("browser", "alice"), "operator")).rejects.toThrow(
      "resume store unavailable",
    );
    expect(expected.raised).toBe(true);
    expect(failing.principalConnectionCount("alice")).toBe(expected.count_after);
  });
});

describe("scanEventsForResume", () => {
  it.each(golden.resume_scans)("$name", (record) => {
    // Checking only the newest event is fragile: a snapshot arriving after an
    // expiry would hide the marker and cause a second resume.
    const state = new WorkerTermState({ now: () => NOW });
    for (const eventType of record.events) {
      state.events.push({ type: eventType });
    }
    expect(scanEventsForResume(state)).toBe(record.resume_needed);
  });

  it("looks past a later snapshot to find the expiry", () => {
    const record = golden.resume_scans.find((entry) => entry.name === "expiry then a snapshot");
    expect(record?.resume_needed).toBe(false);
  });

  it("stops at an acquire rather than scanning past it", () => {
    // An acquire after an expiry means the session was taken again, so the
    // older expiry says nothing about the current state.
    const record = golden.resume_scans.find((entry) => entry.name === "acquired after an expiry");
    expect(record?.resume_needed).toBe(true);
  });
});

describe("a hello against a decided input mode", () => {
  // A `worker_hello` announces what the worker process booted with;
  // `setInputMode` is a decision made through an authenticated route. The hub
  // has to tell them apart, because `inputMode` defaults to `hijack`: a rule
  // refusing every hello that lowers hijack to open would refuse every worker
  // that legitimately announces open, which is most of them.
  function undecided() {
    const { hub, manager } = build();
    hub.registry.put("w1", new WorkerTermState({ now: () => NOW }));
    return { hub, manager };
  }

  it("applies when nobody has decided a mode", () => {
    const { hub, manager } = undecided();
    expect(manager.setWorkerHello("w1", "open")).toBe(true);
    expect(hub.registry.get("w1")?.inputMode).toBe("open");
  });

  it("cannot undo a decision, even with no lease held", () => {
    // The window the lease-only guard left open: an operator sets hijack and
    // then acquires, and a hello landing between the two used to revert the
    // mode — so the acquire was refused for being in open mode, which says
    // nothing about why.
    const { hub, manager } = undecided();
    const state = hub.registry.get("w1");
    if (state === undefined) throw new Error("state");
    state.inputMode = "hijack";
    state.inputModeSetByOperator = true;

    expect(manager.setWorkerHello("w1", "open")).toBe(false);
    expect(hub.registry.get("w1")?.inputMode).toBe("hijack");
  });

  it("may still raise over a decision", () => {
    // One-directional: a worker announcing hijack tells the hub something it
    // does not otherwise know, that automation is driving the session.
    const { hub, manager } = undecided();
    const state = hub.registry.get("w1");
    if (state === undefined) throw new Error("state");
    state.inputMode = "open";
    state.inputModeSetByOperator = true;

    expect(manager.setWorkerHello("w1", "hijack")).toBe(true);
    expect(hub.registry.get("w1")?.inputMode).toBe("hijack");
  });

  it("does not treat agreement with a decided open as a downgrade", () => {
    const { hub, manager } = undecided();
    const state = hub.registry.get("w1");
    if (state === undefined) throw new Error("state");
    state.inputMode = "open";
    state.inputModeSetByOperator = true;

    expect(manager.setWorkerHello("w1", "open")).toBe(true);
  });

  it("holds the decision across repeated reconnects", () => {
    // Why the flag lives on the worker state rather than the connection:
    // registry state outlives a worker socket.
    const { hub, manager } = undecided();
    const state = hub.registry.get("w1");
    if (state === undefined) throw new Error("state");
    state.inputMode = "hijack";
    state.inputModeSetByOperator = true;

    for (let attempt = 0; attempt < 3; attempt++) {
      expect(manager.setWorkerHello("w1", "open")).toBe(false);
    }
    expect(hub.registry.get("w1")?.inputMode).toBe("hijack");
  });
});

describe("ConnectionManager.setWorkerHello", () => {
  it.each(golden.hellos)("$name", (record) => {
    const { hub, manager } = build();
    if (record.exists) {
      hub.registry.put("w1", stateFor(record.rest_expiry, record.has_owner));
    }
    expect(manager.setWorkerHello("w1", record.mode, record.version ?? undefined)).toBe(record.applied);
    const state = hub.registry.get("w1");
    expect(state?.inputMode).toBe(record.input_mode_after ?? undefined);
    expect(state?.protocolVersion).toBe(record.protocol_version_after ?? undefined);
  });

  it("refuses to open input while a session is held", () => {
    // Opening input mid-hijack would let every operator type into a session
    // someone else is driving.
    for (const name of [
      "switch to open while a REST lease is held",
      "switch to open while a dashboard hold is active",
    ]) {
      const record = golden.hellos.find((entry) => entry.name === name);
      expect(record?.applied).toBe(false);
      expect(record?.input_mode_after).toBe("hijack");
    }
  });
});

describe("ConnectionManager.deregisterWorker", () => {
  it.each(golden.deregisters)("$name", (record) => {
    const { hub, manager } = build();
    const ws = socket("worker");
    const state = stateFor(record.rest_expiry, record.has_owner);
    state.workerWs = record.same_socket ? ws : socket("replacement");
    hub.registry.put("w1", state);

    expect(manager.deregisterWorker("w1", ws)).toStrictEqual({
      shouldBroadcast: record.should_broadcast,
      wasHijacked: record.was_hijacked,
    });
    expect(state.workerWs === undefined).toBe(record.worker_cleared);
    expect(state.hijackSession === undefined).toBe(record.session_cleared);
    expect(state.hijackOwner === undefined).toBe(record.owner_cleared);
  });

  it("stays quiet for a socket a replacement already took over from", () => {
    // The replacement is the live worker; tearing its state down because the
    // old socket finally noticed it was closed would disconnect it.
    const record = golden.deregisters.find((entry) => entry.name === "superseded socket");
    expect(record?.should_broadcast).toBe(false);
    expect(record?.worker_cleared).toBe(false);
  });

  it("does nothing for an unknown worker", () => {
    const { manager } = build();
    expect(manager.deregisterWorker("nope", socket("worker"))).toStrictEqual({
      shouldBroadcast: false,
      wasHijacked: false,
    });
  });
});

describe("ConnectionManager.cleanupBrowserDisconnect", () => {
  it.each(golden.disconnects)("$name", async (record) => {
    const { hub, manager } = build((fake) => {
      fake.maxConnectionsPerPrincipal = 5;
    });
    const ws = socket("browser", "alice");
    await manager.registerBrowser("w1", ws, "operator");
    const state = hub.registry.require("w1");
    state.workerWs = socket("worker");
    if (record.rest_expiry !== null) {
      state.hijackSession = { hijackId: "h1", owner: "cli", leaseExpiresAt: record.rest_expiry };
    }
    if (record.is_owner) {
      state.hijackOwner = ws;
      state.hijackOwnerExpiresAt = NOW + 60;
    }

    expect(manager.cleanupBrowserDisconnect("w1", ws, record.owned_hijack)).toStrictEqual({
      wasOwner: record.was_owner,
      restStillActive: record.rest_still_active,
      resumeWithoutOwner: record.resume_without_owner,
    });
    expect(manager.principalConnectionCount("alice")).toBe(record.count_after);
    expect(state.browsers.size).toBe(record.browsers_left);
  });

  it("decrements rather than clearing when a principal has other connections", async () => {
    // The counter has to come down by one, not be dropped: a principal with
    // three tabs who closes one still holds two.
    const { manager } = build((fake) => {
      fake.maxConnectionsPerPrincipal = 5;
    });
    const first = socket("browser-1", "alice");
    const second = socket("browser-2", "alice");
    await manager.registerBrowser("w1", first, "viewer");
    await manager.registerBrowser("w1", second, "viewer");
    manager.cleanupBrowserDisconnect("w1", first, false);
    expect(manager.principalConnectionCount("alice")).toBe(1);
    manager.cleanupBrowserDisconnect("w1", second, false);
    expect(manager.principalConnectionCount("alice")).toBe(0);
  });

  it("defaults to the monotonic clock", () => {
    // Every other test freezes it; a worker registered on the real clock has
    // to get a plausible activity stamp rather than zero.
    const hub = new FakeHub();
    const manager = new ConnectionManager({ hub });
    manager.registerWorker("w1", socket("worker"));
    expect(hub.registry.require("w1").lastActivityAt).toBeGreaterThan(0);
  });

  it("releases the hold when its owner disconnects", async () => {
    const record = golden.disconnects.find((entry) => entry.name === "holder leaves");
    expect(record?.was_owner).toBe(true);
  });

  it("does not release someone else's hold when a viewer leaves", () => {
    // Same identity check on the way out: a viewer disconnecting must not
    // free the session the operator is driving.
    const { hub, manager } = build();
    const state = new WorkerTermState({ now: () => NOW });
    const holder = socket("holder");
    const viewer = socket("viewer");
    state.hijackOwner = holder;
    state.hijackOwnerExpiresAt = NOW + 60;
    state.browsers.set(holder, "operator");
    state.browsers.set(viewer, "viewer");
    hub.registry.put("w1", state);
    expect(manager.cleanupBrowserDisconnect("w1", viewer, false).wasOwner).toBe(false);
    expect(state.hijackOwner).toBe(holder);
  });

  it("copes with a browser whose worker has gone", () => {
    const { manager } = build();
    expect(manager.cleanupBrowserDisconnect("nope", socket("browser"), false)).toStrictEqual({
      wasOwner: false,
      restStillActive: false,
      resumeWithoutOwner: false,
    });
  });

  it("stops treating a browser as pending once it disconnects", async () => {
    const { hub, manager } = build();
    const ws = socket("browser");
    await manager.registerBrowser("w1", ws, "viewer", { deferBroadcast: true });
    expect(hub.startupPendingBrowsers.has(ws)).toBe(true);
    manager.cleanupBrowserDisconnect("w1", ws, false);
    expect(hub.startupPendingBrowsers.has(ws)).toBe(false);
  });
});

describe("ConnectionManager browser registration", () => {
  it("reports the session a browser is joining", async () => {
    const { hub, manager } = build();
    const state = new WorkerTermState({ now: () => NOW });
    state.workerWs = socket("worker");
    state.lastSnapshot = { screen: "hello" };
    hub.registry.put("w1", state);
    const ws = socket("browser");
    expect(await manager.registerBrowser("w1", ws, "operator")).toStrictEqual({
      isHijacked: false,
      hijackedByMe: false,
      workerOnline: true,
      inputMode: "hijack",
      initialSnapshot: { screen: "hello" },
    });
    expect(state.browsers.get(ws)).toBe("operator");
  });

  it("tells a browser that already holds the lease that it is theirs", async () => {
    // A browser reconnecting into a session it still owns must not be told
    // someone else has it.
    const { hub, manager } = build();
    const state = new WorkerTermState({ now: () => NOW });
    hub.registry.put("w1", state);
    const ws = socket("browser");
    state.hijackOwner = ws;
    state.hijackOwnerExpiresAt = NOW + 60;
    const result = await manager.registerBrowser("w1", ws, "operator");
    expect(result.isHijacked).toBe(true);
    expect(result.hijackedByMe).toBe(true);
  });

  it("does not tell a browser the session is theirs when someone else holds it", async () => {
    // The identity check, not merely "is it held": without it every viewer
    // joining a hijacked session is told it is in control.
    const { hub, manager } = build();
    const state = new WorkerTermState({ now: () => NOW });
    state.hijackOwner = socket("someone-else");
    state.hijackOwnerExpiresAt = NOW + 60;
    hub.registry.put("w1", state);
    const result = await manager.registerBrowser("w1", socket("browser"), "viewer");
    expect(result.isHijacked).toBe(true);
    expect(result.hijackedByMe).toBe(false);
  });

  it("creates the worker state for a browser that arrives first", async () => {
    // A browser can open the page before its worker has connected.
    const { hub, manager } = build();
    const result = await manager.registerBrowser("w1", socket("browser"), "viewer");
    expect(result.workerOnline).toBe(false);
    expect(hub.registry.contains("w1")).toBe(true);
  });

  it("holds broadcasts back until the handshake finishes", async () => {
    const { hub, manager } = build();
    const ws = socket("browser");
    await manager.registerBrowser("w1", ws, "viewer", { deferBroadcast: true });
    expect(hub.startupPendingBrowsers.has(ws)).toBe(true);
    manager.activateBrowserBroadcasts("w1", ws);
    expect(hub.startupPendingBrowsers.has(ws)).toBe(false);
  });

  it("does not activate a browser that is no longer attached", async () => {
    // The browser can disconnect between its startup frames and this call.
    const { hub, manager } = build();
    const ws = socket("browser");
    await manager.registerBrowser("w1", ws, "viewer", { deferBroadcast: true });
    hub.registry.require("w1").browsers.delete(ws);
    manager.activateBrowserBroadcasts("w1", ws);
    expect(hub.startupPendingBrowsers.has(ws)).toBe(true);
  });

  it("does not activate for an unknown worker", async () => {
    const { hub, manager } = build();
    const ws = socket("browser");
    await manager.registerBrowser("w1", ws, "viewer", { deferBroadcast: true });
    manager.activateBrowserBroadcasts("nope", ws);
    expect(hub.startupPendingBrowsers.has(ws)).toBe(true);
  });

  it("hands back a resume token when one can be minted", async () => {
    const { manager } = build();
    const withResume = new ConnectionManager({
      hub: new FakeHub(),
      now: () => NOW,
      createResumeToken: async () => "token-1",
    });
    expect((await withResume.registerBrowser("w1", socket("browser"), "viewer")).resumeToken).toBe("token-1");
    expect((await manager.registerBrowser("w1", socket("browser"), "viewer")).resumeToken).toBeUndefined();
  });
});
