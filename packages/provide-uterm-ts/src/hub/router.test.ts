//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  BROADCAST_SEND_TIMEOUT_S,
  encodeBrowserFrame,
  hijackOwnerLabel,
  MessageRouter,
  type RouterHubCallbacks,
  WorkerRegistry,
  WorkerTermState,
} from "./index.ts";
import type { BrowserRole, Connection } from "./models.ts";

interface HubRouterGolden {
  browser_frames: Array<{ name: string; message: Record<string, unknown>; encoded: string }>;
  owners: Array<{
    name: string;
    is_dashboard: boolean;
    is_rest: boolean;
    holds_lease: boolean;
    owner: string | null;
    frame: Record<string, unknown>;
  }>;
}

const golden = loadGolden<HubRouterGolden>("hub_router_golden.json");

/** A browser socket that records what it was sent, and can misbehave. */
class FakeBrowser {
  readonly sent: string[] = [];
  readonly id: string;
  readonly #mode: "ok" | "throw" | "hang";

  constructor(id: string, mode: "ok" | "throw" | "hang" = "ok") {
    this.id = id;
    this.#mode = mode;
  }

  async sendText(payload: string): Promise<void> {
    if (this.#mode === "throw") {
      throw new Error("socket closed");
    }
    if (this.#mode === "hang") {
      await new Promise(() => {});
    }
    this.sent.push(payload);
  }
}

/** A recording stand-in for the hub surface the router reaches through. */
class FakeHub implements RouterHubCallbacks {
  readonly registry = new WorkerRegistry<WorkerTermState>();
  readonly startupPendingBrowsers = new Set<Connection>();
  readonly removedDead: Array<Set<Connection>> = [];
  readonly calls: string[] = [];
  /** What removeDeadBrowsers reports back. */
  deadRemovalChanged = false;

  isHijacked(state: WorkerTermState): boolean {
    return this.isDashboardHijackActive(state) || this.hasValidRestLease(state);
  }

  isDashboardHijackActive(state: WorkerTermState): boolean {
    return state.hijackOwner !== undefined;
  }

  hasValidRestLease(state: WorkerTermState): boolean {
    return state.hijackSession !== undefined;
  }

  async removeDeadBrowsers(_workerId: string, dead: Set<Connection>): Promise<boolean> {
    this.calls.push("remove_dead_browsers");
    this.removedDead.push(dead);
    for (const ws of dead) {
      for (const state of this.registry.all()) {
        state.browsers.delete(ws);
      }
    }
    return this.deadRemovalChanged;
  }
}

/** A router over one worker with the given browsers attached. */
function build(browsers: Array<[FakeBrowser, BrowserRole]> = []) {
  const hub = new FakeHub();
  const state = new WorkerTermState({ now: () => 0 });
  for (const [ws, role] of browsers) {
    state.browsers.set(ws, role);
  }
  hub.registry.put("w1", state);
  const router = new MessageRouter({ hub, sendTimeoutS: 0.05 });
  return { hub, state, router };
}

describe("encodeBrowserFrame", () => {
  it.each(golden.browser_frames)("$name", (record) => {
    // A term message is raw terminal data; anything else is a framed control
    // envelope. Sending a control frame down the terminal path would render
    // its JSON onto the user's screen.
    expect(encodeBrowserFrame(record.message)).toBe(record.encoded);
  });
});

describe("hijackOwnerLabel", () => {
  it.each(golden.owners)("$name", (record) => {
    expect(hijackOwnerLabel(record.is_dashboard, record.is_rest, record.holds_lease)).toBe(record.owner ?? undefined);
  });

  it("calls a REST lease someone else's even for a browser in the owner slot", () => {
    // No browser holds a REST lease, so a stale ws in the slot must not make
    // the UI believe it is in control.
    const record = golden.owners.find((entry) => entry.name === "rest lease, and I hold a stale slot");
    expect(record?.owner).toBe("other");
  });
});

describe("MessageRouter.broadcast", () => {
  it("sends to every attached browser", async () => {
    const a = new FakeBrowser("a");
    const b = new FakeBrowser("b");
    const { router } = build([
      [a, "operator"],
      [b, "viewer"],
    ]);
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(a.sent).toStrictEqual(["hi"]);
    expect(b.sent).toStrictEqual(["hi"]);
  });

  it("does nothing for an unknown worker", async () => {
    const { router } = build();
    await expect(router.broadcast("nope", { type: "term", data: "hi" })).resolves.toBeUndefined();
  });

  it("suppresses an older snapshot after the same worker commits a newer one", async () => {
    const browser = new FakeBrowser("browser");
    const worker = new FakeBrowser("worker");
    const { state, router } = build([[browser, "viewer"]]);
    state.workerWs = worker;
    state.eventSeq = 2;
    state.lastSnapshot = { type: "snapshot", screen: "current", event_seq: 2 };

    await router.broadcast("w1", { type: "snapshot", screen: "old", event_seq: 1 }, worker, 1);

    expect(browser.sent).toStrictEqual([]);
  });

  it("suppresses a snapshot after its worker is replaced without a newer commit", async () => {
    const browser = new FakeBrowser("browser");
    const workerA = new FakeBrowser("worker-a");
    const workerB = new FakeBrowser("worker-b");
    const { state, router } = build([[browser, "viewer"]]);
    state.workerWs = workerB;
    state.eventSeq = 1;
    state.lastSnapshot = { type: "snapshot", screen: "old", event_seq: 1 };

    await router.broadcast("w1", { type: "snapshot", screen: "old", event_seq: 1 }, workerA, 1);

    expect(browser.sent).toStrictEqual([]);
  });

  it("re-checks currency after the egress fence, not just before it", async () => {
    // A snapshot that was current when it arrived can be superseded while it
    // waits its turn behind an earlier one on the egress tail. Checking only
    // on entry would let it reach browsers after the newer screen already did,
    // leaving them showing the older one.
    const browser = new FakeBrowser("browser");
    const worker = new FakeBrowser("worker");
    const { state, router } = build([[browser, "viewer"]]);
    state.workerWs = worker;
    state.eventSeq = 1;
    state.lastSnapshot = { type: "snapshot", screen: "current", event_seq: 1 };

    // Hold the fence open so the broadcast parks on its predecessor.
    let releasePredecessor = () => {};
    state.snapshotEgressTail = new Promise<void>((resolve) => {
      releasePredecessor = resolve;
    });

    const inflight = router.broadcast("w1", { type: "snapshot", screen: "old", event_seq: 1 }, worker, 1);

    // Superseded while parked: a newer snapshot commits behind its back.
    state.eventSeq = 2;
    state.lastSnapshot = { type: "snapshot", screen: "newer", event_seq: 2 };

    releasePredecessor();
    await inflight;

    expect(browser.sent).toStrictEqual([]);
  });

  it("skips browsers still completing their handshake", async () => {
    // A browser mid-startup has not been told what session it is joining, so
    // terminal output arriving first would render before the screen state.
    const a = new FakeBrowser("a");
    const b = new FakeBrowser("b");
    const { hub, router } = build([
      [a, "operator"],
      [b, "viewer"],
    ]);
    hub.startupPendingBrowsers.add(b);
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(a.sent).toStrictEqual(["hi"]);
    expect(b.sent).toStrictEqual([]);
  });

  it("does not let one slow browser delay the others", async () => {
    // Sequential sends would make every later browser wait out the stalled
    // one's whole timeout.
    const slow = new FakeBrowser("slow", "hang");
    const fast = new FakeBrowser("fast");
    const { router } = build([
      [slow, "viewer"],
      [fast, "operator"],
    ]);
    const started = Date.now();
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(fast.sent).toStrictEqual(["hi"]);
    // The whole broadcast costs one timeout, not one per browser.
    expect(Date.now() - started).toBeLessThan(200);
  });

  it("collects a browser that throws as dead", async () => {
    const good = new FakeBrowser("good");
    const bad = new FakeBrowser("bad", "throw");
    const { hub, router } = build([
      [good, "operator"],
      [bad, "viewer"],
    ]);
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(hub.removedDead).toHaveLength(1);
    expect([...(hub.removedDead[0] ?? [])]).toStrictEqual([bad]);
    expect(good.sent).toStrictEqual(["hi"]);
  });

  it("collects a browser that stalls past the timeout as dead", async () => {
    const stalled = new FakeBrowser("stalled", "hang");
    const { hub, router } = build([[stalled, "operator"]]);
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect([...(hub.removedDead[0] ?? [])]).toStrictEqual([stalled]);
  });

  it("does not touch the dead-socket path when everyone is healthy", async () => {
    const good = new FakeBrowser("good");
    const { hub, router } = build([[good, "operator"]]);
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(hub.calls).toStrictEqual([]);
  });

  it("republishes the hijack state when losing a browser changed it", async () => {
    // Losing the lease holder frees the session, and the browsers that are
    // left have to be told.
    const bad = new FakeBrowser("bad", "throw");
    const good = new FakeBrowser("good");
    const { hub, router } = build([
      [bad, "operator"],
      [good, "viewer"],
    ]);
    hub.deadRemovalChanged = true;
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(good.sent.some((frame) => frame.includes("hijack_state"))).toBe(true);
  });

  it("stays quiet when losing a browser changed nothing", async () => {
    const bad = new FakeBrowser("bad", "throw");
    const good = new FakeBrowser("good");
    const { router } = build([
      [bad, "viewer"],
      [good, "operator"],
    ]);
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(good.sent).toStrictEqual(["hi"]);
  });

  it("exposes the send timeout", () => {
    expect(BROADCAST_SEND_TIMEOUT_S).toBeGreaterThan(0);
  });

  it("defaults to the reference timeout", async () => {
    // Every other test shortens it so the stalled-browser cases finish
    // quickly; this pins what a real hub actually uses.
    const good = new FakeBrowser("good");
    const hub = new FakeHub();
    const state = new WorkerTermState({ now: () => 0 });
    state.browsers.set(good, "operator");
    hub.registry.put("w1", state);
    const router = new MessageRouter({ hub });
    await router.broadcast("w1", { type: "term", data: "hi" });
    expect(good.sent).toStrictEqual(["hi"]);
  });
});

describe("MessageRouter.broadcastHijackState", () => {
  it("tells the holder it is theirs and everyone else it is not", async () => {
    const owner = new FakeBrowser("owner");
    const other = new FakeBrowser("other");
    const { state, router } = build([
      [owner, "operator"],
      [other, "viewer"],
    ]);
    state.hijackOwner = owner;
    await router.broadcastHijackState("w1");
    expect(owner.sent[0]).toContain('"owner":"me"');
    expect(other.sent[0]).toContain('"owner":"other"');
  });

  it("reports an idle session as held by nobody", async () => {
    const browser = new FakeBrowser("a");
    const { router } = build([[browser, "viewer"]]);
    await router.broadcastHijackState("w1");
    expect(browser.sent[0]).toContain('"hijacked":false');
    expect(browser.sent[0]).toContain('"owner":null');
  });

  it("shows the REST deadline when a REST lease is held", async () => {
    // Both slots can carry an expiry at once — a dashboard hold that has not
    // been cleared, and the REST lease that actually governs. Showing the
    // stale one would tell the UI the session frees at the wrong moment.
    const browser = new FakeBrowser("a");
    const { state, router } = build([[browser, "viewer"]]);
    state.hijackOwnerExpiresAt = 111;
    state.hijackSession = { hijackId: "h1", owner: "cli", leaseExpiresAt: 222 };
    await router.broadcastHijackState("w1");
    const frame = browser.sent[0] ?? "";
    const shown = Number(/"lease_expires_at":([0-9.eE+-]+)/.exec(frame)?.[1] ?? 0);
    const asWall = (mono: number) => mono + (Date.now() / 1000 - performance.now() / 1000);
    expect(Math.abs(shown - asWall(222))).toBeLessThan(1);
    expect(Math.abs(shown - asWall(111))).toBeGreaterThan(100);
  });

  it("does nothing for an unknown worker", async () => {
    const { router } = build();
    await expect(router.broadcastHijackState("nope")).resolves.toBeUndefined();
  });

  it("skips browsers still completing their handshake", async () => {
    const pending = new FakeBrowser("pending");
    const { hub, router } = build([[pending, "viewer"]]);
    hub.startupPendingBrowsers.add(pending);
    await router.broadcastHijackState("w1");
    expect(pending.sent).toStrictEqual([]);
  });

  it("drops a dead browser and re-sends to the survivors", async () => {
    // The survivors' view of who holds the session may have changed as a
    // result of the dead one going away.
    const bad = new FakeBrowser("bad", "throw");
    const good = new FakeBrowser("good");
    const { hub, state, router } = build([
      [bad, "operator"],
      [good, "viewer"],
    ]);
    state.hijackOwner = bad;
    await router.broadcastHijackState("w1");
    expect(hub.removedDead).toHaveLength(1);
    expect(good.sent.length).toBeGreaterThan(1);
  });

  it("stops if the worker vanishes while dead sockets are cleared", async () => {
    const bad = new FakeBrowser("bad", "throw");
    const { hub, router } = build([[bad, "operator"]]);
    hub.registry.discard("w1");
    await expect(router.broadcastHijackState("w1")).resolves.toBeUndefined();
  });
});
