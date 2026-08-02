//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The hub composition: nine services wired to each other.
 *
 * Each service is proved on its own in `../hub/`, against its own injected
 * callbacks. What is *not* proved there is that this file hands each one the
 * right callback — a hub whose lease manager asked a different predicate than
 * its router would arbitrate one way and report another, and every service's
 * own suite would still pass.
 *
 * So these tests drive the services through the composition and check the
 * answer came back through it: a lease taken through `lease` is one `router`
 * reports, one `presence` sees, and one `connections` refuses to open.
 */

import { describe, expect, it, vi } from "vitest";
import { ControlFrameDecoder } from "../control-channel/index.ts";
import type { WorkerSocket } from "../hub/index.ts";
import { SESSION_HUB_REST_ACQUIRE_RATE, SESSION_HUB_REST_SEND_RATE, SessionHub } from "./session-hub.ts";

/** A worker socket that records every frame the hub sent it. */
function worker(): WorkerSocket & { sent: Record<string, unknown>[] } {
  const sent: Record<string, unknown>[] = [];
  const decoder = new ControlFrameDecoder();
  return {
    sent,
    sendText: async (payload: string) => {
      for (const chunk of decoder.feed(payload)) {
        if (chunk.kind === "control") {
          sent.push(chunk.control);
        }
      }
    },
  };
}

/** A hub with a clock that only moves when a test moves it. */
function hubWithClock() {
  const clock = { mono: 1000, wall: 1_700_000_000 };
  const hub = new SessionHub({
    now: () => clock.mono,
    wallNow: () => clock.wall,
    sleep: async (seconds) => {
      clock.mono += seconds;
    },
  });
  return { hub, clock };
}

/** A hub with a worker already attached, which is what a lease needs. */
function attached(workerId = "w1") {
  const { hub, clock } = hubWithClock();
  const socket = worker();
  hub.registerWorker(workerId, socket, "hijack");
  return { hub, clock, socket, workerId };
}

describe("sending to a worker", () => {
  it("says so when there is nobody to send to", async () => {
    const { hub } = hubWithClock();
    expect(await hub.sendWorker("nobody", { type: "control" })).toBe(false);
  });

  it("frames what it sends, so the worker reads a control frame as one", async () => {
    const { hub, socket } = attached();
    expect(await hub.sendWorker("w1", { type: "control", action: "step" })).toBe(true);
    expect(socket.sent).toEqual([{ type: "control", action: "step" }]);
  });

  it("treats a socket that throws as gone rather than as an error to raise", async () => {
    const { hub } = hubWithClock();
    const closed = {
      sendText: async () => {
        throw new Error("closed");
      },
    };
    hub.registerWorker("w1", closed, "hijack");
    expect(await hub.sendWorker("w1", { type: "control" })).toBe(false);
  });
});

describe("the event log", () => {
  it("hands back an event for a worker that has gone, and stores nothing", async () => {
    const { hub } = hubWithClock();
    const event = await hub.appendEvent("nobody", "hijack_released");
    expect(event).toEqual({ seq: 0, ts: 1_700_000_000, type: "hijack_released", data: {} });
    expect(hub.registry.get("nobody")).toBeUndefined();
  });

  it("numbers events from one and remembers where the log now starts", async () => {
    const { hub, workerId } = attached();
    await hub.appendEvent(workerId, "one");
    const second = await hub.appendEvent(workerId, "two", { k: 1 });
    expect(second).toEqual({ seq: 2, ts: 1_700_000_000, type: "two", data: { k: 1 } });
    expect(hub.registry.get(workerId)?.minEventSeq).toBe(1);
  });
});

describe("committing a snapshot event", () => {
  it("owns the current snapshot and reduced ring event under one sequence", async () => {
    const { hub, workerId } = attached();
    const source = {
      type: "snapshot",
      screen: "one",
      cursor: { x: 2, y: 3 },
      screen_hash: "h1",
      prompt_detected: { prompt_id: "command" },
    };

    const committed = await hub.commitSnapshotEvent(workerId, source);
    const state = hub.registry.get(workerId);
    const event = state?.events.at(0);

    expect(committed).toEqual({ ...source, event_seq: 1 });
    expect(state?.lastSnapshot).toEqual(committed);
    expect(event).toEqual({
      seq: 1,
      ts: 1_700_000_000,
      type: "snapshot",
      data: { prompt_id: "command", screen_hash: "h1", screen: "one", event_seq: 1 },
    });
    expect(state?.lastSnapshot).not.toBe(committed);
    expect(event?.data).not.toBe(committed);

    source.cursor.x = 99;
    (committed.cursor as Record<string, number>).x = 88;
    expect((state?.lastSnapshot?.cursor as Record<string, number>).x).toBe(2);
  });

  it("pairs concurrent snapshot commits with unique monotonic sequences", async () => {
    const { hub, workerId } = attached();
    const committed = await Promise.all(
      Array.from({ length: 24 }, (_unused, index) =>
        hub.commitSnapshotEvent(workerId, {
          type: "snapshot",
          screen: `screen-${index}`,
          screen_hash: `hash-${index}`,
          prompt_detected: null,
        }),
      ),
    );
    const state = hub.registry.get(workerId);
    const events = state?.events.toArray() ?? [];

    expect(committed.map((frame) => frame.event_seq)).toEqual(Array.from({ length: 24 }, (_, index) => index + 1));
    expect(events.map((event) => event.seq)).toEqual(Array.from({ length: 24 }, (_, index) => index + 1));
    for (const [index, frame] of committed.entries()) {
      expect(events[index]).toMatchObject({
        seq: frame.event_seq,
        data: {
          screen: frame.screen,
          screen_hash: frame.screen_hash,
          event_seq: frame.event_seq,
        },
      });
    }
    expect(state?.lastSnapshot).toEqual(committed.at(-1));
    expect(state?.lastSnapshot).not.toBe(committed.at(-1));
  });
});

describe("the last screen", () => {
  it("keeps nothing for a worker it does not know", async () => {
    const { hub } = hubWithClock();
    await hub.updateLastSnapshot("nobody", { screen: "x" });
    expect(await hub.getLastSnapshot("nobody")).toBeUndefined();
  });

  it("hands back what the worker last sent", async () => {
    const { hub, workerId } = attached();
    await hub.updateLastSnapshot(workerId, { screen: "x" });
    expect(await hub.getLastSnapshot(workerId)).toEqual({ screen: "x" });
  });
});

describe("the input mode", () => {
  it("refuses a worker nobody has attached", async () => {
    const { hub } = hubWithClock();
    expect(await hub.setInputMode("nobody", "open")).toEqual({ ok: false, reason: "not_found" });
  });

  it("refuses to open a session somebody is holding", async () => {
    const { hub, workerId, clock } = attached();
    await hub.lease.tryAcquireRest(workerId, { owner: "o", leaseSeconds: 60, hijackId: "h", now: clock.mono });
    expect(await hub.setInputMode(workerId, "open")).toEqual({ ok: false, reason: "active_hijack" });
  });

  it("tells the worker table, which is what an acquire is refused against", async () => {
    const { hub, workerId, clock } = attached();
    expect(await hub.setInputMode(workerId, "open")).toEqual({ ok: true });
    expect(
      await hub.lease.tryAcquireRest(workerId, { owner: "o", leaseSeconds: 60, hijackId: "h", now: clock.mono }),
    ).toEqual({ ok: false, reason: "open_mode" });
  });
});

describe("forcing a lease off", () => {
  it("reports nothing to do for a worker nobody has attached", async () => {
    const { hub } = hubWithClock();
    expect(await hub.forceReleaseHijack("nobody")).toBe(false);
  });

  it("reports nothing to do when nothing is held", async () => {
    const { hub, workerId } = attached();
    expect(await hub.forceReleaseHijack(workerId)).toBe(false);
  });

  it("clears the REST lease and lets the worker run again", async () => {
    const { hub, workerId, socket, clock } = attached();
    await hub.lease.tryAcquireRest(workerId, { owner: "held", leaseSeconds: 60, hijackId: "h", now: clock.mono });
    socket.sent.length = 0;

    expect(await hub.forceReleaseHijack(workerId)).toBe(true);

    expect(socket.sent).toEqual([{ type: "control", action: "resume", owner: "held", lease_s: 0, ts: 1_700_000_000 }]);
    expect(hub.registry.get(workerId)?.hijackSession).toBeUndefined();
    expect(hub.registry.get(workerId)?.hijackOwner).toBeUndefined();
  });

  it("clears a dashboard hold that no REST lease accompanies", async () => {
    const { hub, workerId } = attached();
    await hub.lease.tryAcquireWs(workerId, {});
    expect(await hub.forceReleaseHijack(workerId)).toBe(true);
  });
});

describe("forgetting an idle worker", () => {
  it("does nothing for a worker it does not know", async () => {
    const { hub } = hubWithClock();
    await hub.pruneIfIdle("nobody");
    expect(hub.registry.size).toBe(0);
  });

  it("keeps one whose socket is still attached", async () => {
    const { hub, workerId } = attached();
    await hub.pruneIfIdle(workerId);
    expect(hub.registry.contains(workerId)).toBe(true);
  });

  it("keeps one a browser is still watching", async () => {
    const { hub, workerId, socket } = attached();
    await hub.connections.registerBrowser(workerId, {}, "viewer");
    hub.connections.deregisterWorker(workerId, socket);
    await hub.pruneIfIdle(workerId);
    expect(hub.registry.contains(workerId)).toBe(true);
  });

  it("keeps one whose socket dropped but whose lease has not", async () => {
    // A socket that died without a clean deregister leaves exactly this: no
    // worker, and a lease its holder still believes in.
    const { hub, workerId, clock } = attached();
    await hub.lease.tryAcquireRest(workerId, { owner: "o", leaseSeconds: 60, hijackId: "h", now: clock.mono });
    (hub.registry.get(workerId) as { workerWs: unknown }).workerWs = undefined;
    await hub.pruneIfIdle(workerId);
    expect(hub.registry.contains(workerId)).toBe(true);
  });

  it("keeps one a dashboard hold outlives", async () => {
    const { hub, workerId, socket } = attached();
    await hub.lease.tryAcquireWs(workerId, {});
    (hub.registry.get(workerId) as { workerWs: unknown }).workerWs = undefined;
    void socket;
    await hub.pruneIfIdle(workerId);
    expect(hub.registry.contains(workerId)).toBe(true);
  });

  it("forgets one with nothing left at all", async () => {
    const { hub, workerId, socket } = attached();
    hub.connections.deregisterWorker(workerId, socket);
    await hub.pruneIfIdle(workerId);
    expect(hub.registry.contains(workerId)).toBe(false);
  });
});

describe("a lease deadline, as whoever reads it reads it", () => {
  it("carries the offset between the two clocks", () => {
    const { hub, clock } = attached();
    expect(hub.monoToWall(clock.mono + 60)).toBe(1_700_000_060);
    expect(hub.monotonic()).toBe(clock.mono);
    expect(hub.wallNow()).toBe(clock.wall);
  });

  it("holds a requested lease inside the range it grants", () => {
    const { hub } = hubWithClock();
    expect(hub.clampLease(90.9)).toBe(90);
    expect(hub.clampLease(0)).toBe(1);
    expect(hub.clampLease(1e9)).toBe(14400);
  });
});

describe("each service asking the composition, and getting the composition's answer", () => {
  it("reports a REST lease to a browser joining, and to the frames it is sent", async () => {
    const { hub, workerId, clock } = attached();
    const browser = { sendText: vi.fn(async () => undefined) };
    await hub.lease.tryAcquireRest(workerId, { owner: "o", leaseSeconds: 60, hijackId: "h", now: clock.mono });

    const joined = await hub.connections.registerBrowser(workerId, browser, "operator");
    expect(joined).toMatchObject({ isHijacked: true, hijackedByMe: false, workerOnline: true });
    expect(await hub.presence.registerBrowserStateSnapshot(workerId, browser)).toMatchObject({ isHijacked: true });

    await hub.router.broadcastHijackState(workerId);
    expect(browser.sendText).toHaveBeenCalled();
  });

  it("resolves every connection to a viewer, because no browser can attach", async () => {
    const { hub, workerId } = attached();
    expect(await hub.presence.resolveRoleForBrowser({}, workerId)).toBe("viewer");
  });

  it("lets the dashboard holder type, and refuses everyone else", async () => {
    const { hub, workerId } = attached();
    const holder = {};
    await hub.connections.registerBrowser(workerId, holder, "operator");
    await hub.lease.tryAcquireWs(workerId, holder);
    expect(await hub.lease.prepareBrowserInput(workerId, holder)).toBe(true);
    expect(await hub.lease.prepareBrowserInput(workerId, {})).toBe(false);
  });

  it("asks the worker for a screen through the same socket the lease pauses it on", async () => {
    const { hub, workerId, socket } = attached();
    await hub.presence.requestSnapshot(workerId);
    expect(socket.sent[0]).toMatchObject({ type: "snapshot_req" });
  });

  it("expires a lapsed lease, resumes the worker and writes it down", async () => {
    const { hub, workerId, socket, clock } = attached();
    await hub.lease.tryAcquireRest(workerId, { owner: "o", leaseSeconds: 60, hijackId: "h", now: clock.mono });
    socket.sent.length = 0;
    clock.mono += 3600;

    expect(await hub.lease.cleanupExpired(workerId)).toBe(true);

    // The resume went out through the composition's `sendWorker`, and the two
    // expiries were written through its `appendEvent`.
    expect(socket.sent).toEqual([
      { type: "control", action: "resume", owner: "lease-expired", lease_s: 0, ts: clock.mono },
    ]);
    const events = hub.registry.get(workerId)?.events.toArray() ?? [];
    expect(events.map((event) => event.type)).toEqual(["hijack_lease_expired"]);
    expect(await hub.lease.stillHijacked(workerId)).toBe(false);
  });

  it("drops a browser whose socket has died, and tells the survivors", async () => {
    const { hub, workerId } = attached();
    const dead = {
      sendText: async () => {
        throw new Error("gone");
      },
    };
    await hub.connections.registerBrowser(workerId, dead, "operator");
    await hub.lease.tryAcquireWs(workerId, dead);

    await hub.router.broadcast(workerId, { type: "term", data: "x" });

    expect(hub.registry.get(workerId)?.browsers.size).toBe(0);
    expect(hub.registry.get(workerId)?.hijackOwner).toBeUndefined();
  });

  it("refuses to open a session at the worker's hello while one is held", async () => {
    const { hub, workerId, clock } = attached();
    await hub.lease.tryAcquireRest(workerId, { owner: "o", leaseSeconds: 60, hijackId: "h", now: clock.mono });
    expect(hub.connections.setWorkerHello(workerId, "open")).toBe(false);
  });

  it("reports a surviving REST lease when the browser that held the dashboard leaves", async () => {
    const { hub, workerId, clock } = attached();
    const holder = {};
    await hub.connections.registerBrowser(workerId, holder, "operator");
    await hub.lease.tryAcquireWs(workerId, holder);
    // The REST lease is written directly: only one of the two can be *taken*
    // at a time, and this is the state a browser leaving has to report on.
    (hub.registry.get(workerId) as { hijackSession: unknown }).hijackSession = {
      hijackId: "h",
      owner: "o",
      leaseExpiresAt: clock.mono + 60,
    };

    expect(hub.connections.cleanupBrowserDisconnect(workerId, holder, true)).toEqual({
      wasOwner: true,
      restStillActive: true,
      resumeWithoutOwner: false,
    });
  });
});

describe("the REST rate ceilings the hub runs on", () => {
  /** How many calls a policy allows before it starts refusing. */
  function budget(allow: () => boolean): number {
    let allowed = 0;
    // One more than any rate under test, so the count is the budget and not
    // the loop bound.
    for (let attempt = 0; attempt < 200; attempt += 1) {
      if (allow()) {
        allowed += 1;
      }
    }
    return allowed;
  }

  it("runs on the reference's own defaults when the configuration says nothing", () => {
    // A deployment that has never heard of these keys must behave exactly as
    // it did before they existed.
    const hub = new SessionHub({ now: () => 1000 });
    expect(hub.limiter.restAcquireRate).toBe(SESSION_HUB_REST_ACQUIRE_RATE);
    expect(hub.limiter.restSendRate).toBe(SESSION_HUB_REST_SEND_RATE);
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(5);
    expect(budget(() => hub.limiter.allowRestSend("a"))).toBe(20);
  });

  it("takes the acquire ceiling from the configuration and leaves send alone", () => {
    const hub = new SessionHub({ now: () => 1000, restAcquireRate: 2 });
    expect(hub.limiter.restAcquireRate).toBe(2);
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(2);
    expect(budget(() => hub.limiter.allowRestSend("a"))).toBe(SESSION_HUB_REST_SEND_RATE);
  });

  it("takes the send ceiling from the configuration and leaves acquire alone", () => {
    const hub = new SessionHub({ now: () => 1000, restSendRate: 3 });
    expect(hub.limiter.restSendRate).toBe(3);
    expect(budget(() => hub.limiter.allowRestSend("a"))).toBe(3);
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(SESSION_HUB_REST_ACQUIRE_RATE);
  });

  it("refills a configured ceiling at the configured rate", () => {
    // The rate is a rate and not just a burst: a client that waits earns
    // exactly what it configured back, on the hub's own monotonic clock.
    const clock = { mono: 1000 };
    const hub = new SessionHub({ now: () => clock.mono, restAcquireRate: 2 });
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(2);
    clock.mono += 1;
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(2);
  });

  it("floors a sub-1 rate rather than running a policy that admits nothing", () => {
    // Belt and braces. The schema refuses anything under 1/s at config load —
    // below the floor a bucket's ceiling sits under the whole token a call
    // costs, so it would admit nothing however long the caller waits — but a
    // caller that constructs a hub directly bypasses the schema entirely, and
    // gets the floor rather than a bricked endpoint. The bucket property
    // itself is pinned in `../ratelimit/`, where a starved bucket can still be
    // built.
    const hub = new SessionHub({ now: () => 1000, restAcquireRate: 0.5, restSendRate: 0 });
    expect(hub.limiter.restAcquireRate).toBe(1);
    expect(hub.limiter.restSendRate).toBe(1);
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(1);
  });

  it("hands one call a second out at the floor itself", () => {
    const clock = { mono: 1000 };
    const hub = new SessionHub({ now: () => clock.mono, restAcquireRate: 1 });
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(1);
    clock.mono += 1;
    expect(budget(() => hub.limiter.allowRestAcquire("a"))).toBe(1);
  });
});
