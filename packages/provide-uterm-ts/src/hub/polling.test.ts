//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { PollingCoordinator, type PollingHubCallbacks, WorkerRegistry, WorkerTermState } from "./index.ts";

/**
 * A hub whose clock and sleep are driven by the test.
 *
 * Real time would make these tests slow and flaky in equal measure — the
 * behaviour under test is *how many* polls happen and *when* the worker is
 * nudged, which is exactly what a wall-clock timeout blurs.
 */
class FakeHub implements PollingHubCallbacks {
  readonly registry = new WorkerRegistry<WorkerTermState>();
  readonly snapshotRequests: string[] = [];
  /** Monotonic seconds; each sleep advances it. */
  now = 1000;
  /** Every sleep duration asked for, in seconds. */
  readonly sleeps: number[] = [];
  /** Runs before each sleep returns, to mutate state mid-poll. */
  onSleep: (() => void) | undefined;

  async requestSnapshot(workerId: string): Promise<void> {
    this.snapshotRequests.push(workerId);
  }

  monotonic(): number {
    return this.now;
  }

  async sleep(seconds: number): Promise<void> {
    this.sleeps.push(seconds);
    this.now += seconds;
    this.onSleep?.();
  }
}

/** A coordinator over a hub with one worker in it. */
function build(lastSnapshot?: Record<string, unknown>) {
  const hub = new FakeHub();
  const state = new WorkerTermState({ now: () => hub.now });
  state.lastSnapshot = lastSnapshot;
  hub.registry.put("w1", state);
  const polling = new PollingCoordinator({ hub, wallNow: () => hub.now });
  return { hub, state, polling };
}

describe("PollingCoordinator.waitForSnapshot", () => {
  it("asks the worker before it starts polling", async () => {
    // Otherwise the first poll can only ever see whatever was already there.
    const { hub, polling } = build();
    await polling.waitForSnapshot("w1", 100);
    expect(hub.snapshotRequests[0]).toBe("w1");
  });

  it("returns a snapshot newer than the request", async () => {
    const { hub, state, polling } = build();
    hub.onSleep = () => {
      state.lastSnapshot = { ts: hub.now + 1, screen: "fresh" };
    };
    expect(await polling.waitForSnapshot("w1", 1000)).toStrictEqual({ ts: hub.now + 1, screen: "fresh" });
  });

  it("ignores a snapshot older than the request", async () => {
    // A stale snapshot is the common case — the worker has been sitting
    // there since before the caller asked — and returning it would answer
    // the wrong question.
    const { hub, polling } = build({ ts: 0, screen: "stale" });
    expect(await polling.waitForSnapshot("w1", 100)).toBeUndefined();
    expect(hub.sleeps.length).toBeGreaterThan(0);
  });

  it("ignores a snapshot stamped before the request", async () => {
    // Not merely "before the epoch": a snapshot from a second ago is the
    // realistic stale case, and it must not be mistaken for an answer.
    const { hub, polling } = build({ ts: 999, screen: "a second ago" });
    expect(hub.now).toBe(1000);
    expect(await polling.waitForSnapshot("w1", 100)).toBeUndefined();
  });

  it("ignores a snapshot stamped exactly at the request", async () => {
    // The comparison is strict, so a snapshot taken in the same instant as
    // the request does not count as a reply to it.
    const { hub, polling } = build({ ts: 1000, screen: "same instant" });
    expect(hub.now).toBe(1000);
    expect(await polling.waitForSnapshot("w1", 100)).toBeUndefined();
  });

  it("ignores a snapshot with no timestamp", async () => {
    const { polling } = build({ screen: "no ts" });
    expect(await polling.waitForSnapshot("w1", 100)).toBeUndefined();
  });

  it("gives up when the worker disappears", async () => {
    const { hub, polling } = build();
    hub.registry.discard("w1");
    expect(await polling.waitForSnapshot("w1", 1000)).toBeUndefined();
    // No point sleeping out the timeout for a worker that has gone.
    expect(hub.sleeps).toStrictEqual([]);
  });

  it("stops once the deadline passes", async () => {
    const { hub, polling } = build({ ts: 0 });
    await polling.waitForSnapshot("w1", 250);
    const slept = hub.sleeps.reduce((total, seconds) => total + seconds, 0);
    expect(slept).toBeGreaterThanOrEqual(0.25);
    expect(slept).toBeLessThan(0.25 + 0.08);
  });

  it("polls no more than once per interval", async () => {
    const { hub, polling } = build({ ts: 0 });
    await polling.waitForSnapshot("w1", 1000);
    expect(new Set(hub.sleeps)).toStrictEqual(new Set([0.08]));
  });
});

describe("PollingCoordinator.waitForGuard", () => {
  it("refuses an unsafe guard without polling at all", async () => {
    // The refusal has to come back to the caller as a reason, not as a
    // timeout: they need to know the pattern was rejected, not that the
    // screen never matched.
    const { hub, polling } = build();
    const result = await polling.waitForGuard("w1", {
      expectRegex: "(a+)+",
      timeoutMs: 1000,
      pollIntervalMs: 50,
    });
    expect(result.matched).toBe(false);
    expect(result.reason).toContain("unsafe");
    expect(hub.sleeps).toStrictEqual([]);
    expect(hub.snapshotRequests).toStrictEqual([]);
  });

  it("rethrows an error that is not a pattern refusal", async () => {
    // A bad pattern is the caller's problem and comes back as a reason. A
    // compiler blowing up some other way is our bug, and must not be
    // disguised as "your regex was rejected".
    const { polling: _unused, hub } = build();
    const polling = new PollingCoordinator({
      hub,
      wallNow: () => hub.now,
      compileGuard: () => {
        throw new TypeError("compiler exploded");
      },
    });
    await expect(polling.waitForGuard("w1", { expectRegex: "x", timeoutMs: 100, pollIntervalMs: 50 })).rejects.toThrow(
      TypeError,
    );
  });

  it("refuses an over-long guard", async () => {
    const { polling } = build();
    const result = await polling.waitForGuard("w1", {
      expectRegex: "a".repeat(201),
      timeoutMs: 1000,
      pollIntervalMs: 50,
    });
    expect(result.reason).toContain("too long");
  });

  it("returns immediately when there is nothing to wait for", async () => {
    // No guards means the caller only wanted the current screen; polling for
    // a condition that is already satisfied would just add latency.
    const { hub, polling, state } = build({ ts: 5, screen: "whatever" });
    const result = await polling.waitForGuard("w1", { timeoutMs: 1000, pollIntervalMs: 50 });
    expect(result).toStrictEqual({ matched: true, snapshot: state.lastSnapshot });
    expect(hub.sleeps).toStrictEqual([]);
    // It still nudges the worker, so the *next* caller sees something fresh.
    expect(hub.snapshotRequests).toStrictEqual(["w1"]);
  });

  it("returns nothing for an unknown worker when there is nothing to wait for", async () => {
    const { hub, polling } = build();
    hub.registry.discard("w1");
    const result = await polling.waitForGuard("w1", { timeoutMs: 1000, pollIntervalMs: 50 });
    expect(result).toStrictEqual({ matched: true, snapshot: undefined });
  });

  it("matches on a prompt id", async () => {
    const { polling } = build({ ts: 1, prompt_detected: { prompt_id: "bash" } });
    const result = await polling.waitForGuard("w1", {
      expectPromptId: "bash",
      timeoutMs: 1000,
      pollIntervalMs: 50,
    });
    expect(result.matched).toBe(true);
  });

  it("matches on a regex once the screen catches up", async () => {
    const { hub, state, polling } = build({ ts: 1, screen: "booting" });
    hub.onSleep = () => {
      state.lastSnapshot = { ts: hub.now, screen: "system ready" };
    };
    const result = await polling.waitForGuard("w1", {
      expectRegex: "ready",
      timeoutMs: 1000,
      pollIntervalMs: 50,
    });
    expect(result).toStrictEqual({ matched: true, snapshot: { ts: hub.now, screen: "system ready" } });
  });

  it("gives up with a reason when the guard never holds", async () => {
    const { polling } = build({ ts: 1, screen: "booting" });
    const result = await polling.waitForGuard("w1", {
      expectRegex: "ready",
      timeoutMs: 200,
      pollIntervalMs: 50,
    });
    expect(result.matched).toBe(false);
    expect(result.reason).toBe("prompt_guard_not_satisfied");
    // The last thing it saw comes back, so the caller can see how far it got.
    expect(result.snapshot).toStrictEqual({ ts: 1, screen: "booting" });
  });

  it("nudges the worker again only when the snapshot has not advanced", async () => {
    // A worker already streaming snapshots must not be flooded with
    // requests; one that has gone quiet must be poked.
    const { hub, state, polling } = build({ ts: 1, screen: "booting" });
    let tick = 1;
    hub.onSleep = () => {
      tick += 1;
      state.lastSnapshot = { ts: tick, screen: "booting" };
    };
    await polling.waitForGuard("w1", { expectRegex: "ready", timeoutMs: 200, pollIntervalMs: 50 });
    // Only the initial request: every poll saw a newer snapshot than the last.
    expect(hub.snapshotRequests).toStrictEqual(["w1"]);
  });

  it("treats a snapshot with no timestamp as not having advanced", async () => {
    // Otherwise an untimed snapshot would look like progress on every poll
    // and the worker would never be nudged again.
    const { hub, polling } = build({ screen: "booting" });
    await polling.waitForGuard("w1", { expectRegex: "ready", timeoutMs: 200, pollIntervalMs: 50 });
    expect(hub.snapshotRequests.length).toBeGreaterThan(1);
  });

  it("nudges on every poll while the snapshot is frozen", async () => {
    const { hub, polling } = build({ ts: 1, screen: "booting" });
    await polling.waitForGuard("w1", { expectRegex: "ready", timeoutMs: 200, pollIntervalMs: 50 });
    expect(hub.snapshotRequests.length).toBeGreaterThan(1);
  });

  it("floors the timeout and the interval", async () => {
    // A caller asking for a 1ms timeout would otherwise get zero polls, and
    // a 1ms interval would spin the loop against the worker. The floors are
    // 50ms and 20ms, so this asks for one and gets three polls.
    const { hub, polling } = build({ ts: 1, screen: "booting" });
    await polling.waitForGuard("w1", { expectRegex: "ready", timeoutMs: 1, pollIntervalMs: 1 });
    expect(hub.sleeps).toStrictEqual([0.02, 0.02, 0.02]);
    expect(hub.sleeps.reduce((total, seconds) => total + seconds, 0)).toBeGreaterThanOrEqual(0.05);
  });

  it("copes with the worker disappearing mid-poll", async () => {
    const { hub, polling } = build({ ts: 1, screen: "booting" });
    hub.onSleep = () => {
      hub.registry.discard("w1");
    };
    const result = await polling.waitForGuard("w1", {
      expectRegex: "ready",
      timeoutMs: 200,
      pollIntervalMs: 50,
    });
    expect(result.matched).toBe(false);
    expect(result.snapshot).toBeUndefined();
  });
});

describe("PollingCoordinator defaults", () => {
  it("compares against wall time by default", async () => {
    // The snapshot timestamp comes from the worker's wall clock, so the
    // request marker has to be wall time too — comparing it against a
    // monotonic reading would make every snapshot look newer or older
    // depending on how long the process had been up.
    const hub = new FakeHub();
    const state = new WorkerTermState();
    state.lastSnapshot = { ts: Date.now() / 1000 + 60, screen: "fresh" };
    hub.registry.put("w1", state);
    const polling = new PollingCoordinator({ hub });
    expect(await polling.waitForSnapshot("w1", 1000)).toBe(state.lastSnapshot);
  });

  it("rejects a snapshot stamped before now under the default clock", async () => {
    const hub = new FakeHub();
    const state = new WorkerTermState();
    state.lastSnapshot = { ts: 0, screen: "ancient" };
    hub.registry.put("w1", state);
    const polling = new PollingCoordinator({ hub });
    expect(await polling.waitForSnapshot("w1", 100)).toBeUndefined();
  });
});
