//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it, vi } from "vitest";
import { noopLogger } from "../telemetry/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  clampLease,
  type HijackSession,
  LEASE_MAX_SECONDS,
  LEASE_MIN_SECONDS,
  StateStore,
  type StateStoreOptions,
  WorkerRegistry,
  WorkerTermState,
} from "./index.ts";

interface HubStoreGolden {
  now: number;
  max_buffer_chars: number;
  buffers: Array<{
    name: string;
    prior: string;
    data: string;
    command: string | null;
    buffered_after: string | null;
  }>;
  buffer_isolation: { first: string; second: string };
  clamps: Array<{ name: string; lease_s: number; clamped: number }>;
  hijack: Array<{
    name: string;
    has_owner: boolean;
    owner_expires_at: number | null;
    session_expires_at: number | null;
    has_valid_rest_lease: boolean;
    is_dashboard_hijack_active: boolean;
    is_hijacked: boolean;
    lease_view_is_dashboard_active: boolean;
  }>;
  metric: { seen: Array<[string, number]> };
  notify: {
    sync_raise_propagates: boolean;
    seen: Array<[string, boolean, string | null]>;
    async_callback_ran_before_yield: boolean;
  };
}

const golden = loadGolden<HubStoreGolden>("hub_store_golden.json");
const NOW = golden.now;

/** A store on a frozen clock, with the recorded buffer cap. */
function makeStore(options: Partial<StateStoreOptions> = {}): StateStore {
  return new StateStore({
    registry: new WorkerRegistry<WorkerTermState>(),
    maxBufferChars: golden.max_buffer_chars,
    now: () => NOW,
    ...options,
  });
}

/** A worker state carrying the hijack slots the recorded cases vary. */
function stateFor(hasOwner: boolean, ownerExpiresAt: number | null, sessionExpiresAt: number | null): WorkerTermState {
  const state = new WorkerTermState({ now: () => NOW });
  if (hasOwner) {
    state.hijackOwner = { id: "ws" };
    state.hijackOwnerExpiresAt = ownerExpiresAt ?? undefined;
  }
  if (sessionExpiresAt !== null) {
    const session: HijackSession = { hijackId: "h1", owner: "operator", leaseExpiresAt: sessionExpiresAt };
    state.hijackSession = session;
  }
  return state;
}

describe("StateStore input buffer", () => {
  it.each(golden.buffers)("$name", (record) => {
    // Overflow discards the whole buffer rather than truncating or flushing
    // it, so an over-long paste vanishes instead of arriving in pieces.
    const store = makeStore();
    const ws = { id: "ws" };
    if (record.prior !== "") {
      store.bufferAndGetCommand(ws, record.prior);
    }
    expect(store.bufferAndGetCommand(ws, record.data)).toBe(record.command ?? undefined);
    expect(store.bufferedFor(ws)).toBe(record.buffered_after ?? undefined);
  });

  it("keeps a separate buffer per browser", () => {
    const store = makeStore();
    const first = { id: "ws1" };
    const second = { id: "ws2" };
    store.bufferAndGetCommand(first, "one");
    store.bufferAndGetCommand(second, "two");
    expect(store.bufferAndGetCommand(first, "\n")).toBe(golden.buffer_isolation.first);
    expect(store.bufferAndGetCommand(second, "\n")).toBe(golden.buffer_isolation.second);
  });

  it("drops a browser's buffer when it disconnects", () => {
    // Otherwise a closed connection's partial line is retained forever, and
    // is handed to whichever object later occupies the same identity.
    const store = makeStore();
    const ws = { id: "ws" };
    store.bufferAndGetCommand(ws, "partial");
    store.dropBuffer(ws);
    expect(store.bufferedFor(ws)).toBeUndefined();
    expect(store.bufferAndGetCommand(ws, "\n")).toBe("\n");
  });
});

describe("clampLease", () => {
  it.each(golden.clamps)("$name", (record) => {
    expect(clampLease(record.lease_s)).toBe(record.clamped);
  });

  it("exposes the reference bounds", () => {
    expect(LEASE_MIN_SECONDS).toBe(golden.clamps.find((c) => c.name === "at the floor")?.clamped);
    expect(LEASE_MAX_SECONDS).toBe(golden.clamps.find((c) => c.name === "at the ceiling")?.clamped);
  });

  it("truncates a fractional lease toward zero", () => {
    // The reference coerces with int(); a lease of 90.9s must not become 91.
    expect(clampLease(90.9)).toBe(90);
    expect(clampLease(1.5)).toBe(1);
  });
});

describe("StateStore hijack predicates", () => {
  it.each(golden.hijack)("$name", (record) => {
    const store = makeStore();
    const state = stateFor(record.has_owner, record.owner_expires_at, record.session_expires_at);
    expect(store.hasValidRestLease(state)).toBe(record.has_valid_rest_lease);
    expect(store.isDashboardHijackActive(state)).toBe(record.is_dashboard_hijack_active);
    expect(store.isHijacked(state)).toBe(record.is_hijacked);
  });

  it("defaults to the monotonic clock", () => {
    // Every other test injects a clock; this pins that the default is real
    // monotonic time and not, say, a zero that makes every lease look live.
    const store = new StateStore({
      registry: new WorkerRegistry<WorkerTermState>(),
      maxBufferChars: golden.max_buffer_chars,
    });
    const state = new WorkerTermState();
    state.hijackSession = { hijackId: "h1", owner: "cli", leaseExpiresAt: 0 };
    expect(store.hasValidRestLease(state)).toBe(false);
    state.hijackSession = { hijackId: "h1", owner: "cli", leaseExpiresAt: Number.MAX_SAFE_INTEGER };
    expect(store.hasValidRestLease(state)).toBe(true);
  });

  it("treats an owner with no expiry as holding a perpetual hijack", () => {
    // The store and the lease view genuinely disagree here, and both answers
    // are in the reference: an owner with no expiry is active to the store
    // and inactive to the view. Unifying them would change who may send
    // input, so the divergence is pinned rather than smoothed over.
    const record = golden.hijack.find((entry) => entry.name === "dashboard owner, no expiry");
    expect(record?.is_dashboard_hijack_active).toBe(true);
    expect(record?.lease_view_is_dashboard_active).toBe(false);

    const store = makeStore();
    const state = stateFor(true, null, null);
    expect(store.isDashboardHijackActive(state)).toBe(true);
    expect(state.lease.isDashboardActive(NOW)).toBe(false);
  });
});

describe("StateStore worker lifecycle", () => {
  it("creates a worker state on first use and reuses it after", () => {
    const registry = new WorkerRegistry<WorkerTermState>();
    const store = makeStore({ registry });
    const created = store.getOrCreate("w1");
    expect(registry.get("w1")).toBe(created);
    expect(store.getOrCreate("w1")).toBe(created);
    expect(registry.size).toBe(1);
  });

  it("stamps activity on a known worker", () => {
    const registry = new WorkerRegistry<WorkerTermState>();
    let clock = NOW;
    const store = new StateStore({
      registry,
      maxBufferChars: golden.max_buffer_chars,
      now: () => clock,
    });
    const state = store.getOrCreate("w1");
    clock = NOW + 60;
    store.touchActivity("w1");
    expect(state.lastActivityAt).toBe(NOW + 60);
  });

  it("does nothing when stamping an unknown worker", () => {
    // The worker can disconnect between a frame arriving and being handled,
    // so this must not create state for a worker that has gone.
    const registry = new WorkerRegistry<WorkerTermState>();
    const store = makeStore({ registry });
    expect(() => store.touchActivity("nope")).not.toThrow();
    expect(registry.size).toBe(0);
  });
});

describe("StateStore metric fan-out", () => {
  it("does nothing when no callback is configured", () => {
    expect(() => makeStore().metric("never_seen")).not.toThrow();
  });

  it("passes the name and a truncated integer value", () => {
    const seen: Array<[string, number]> = [];
    const store = makeStore({ onMetric: (name, value) => seen.push([name, value]) });
    store.metric("default_value");
    store.metric("explicit", 5);
    // The reference coerces with int(), which truncates toward zero.
    store.metric("truncated", 2.9);
    store.metric("negative", -3);
    expect(seen).toStrictEqual(golden.metric.seen);
  });

  it("swallows a throwing callback", () => {
    // A metrics sink is observability, not control flow: it must not be able
    // to tear down the session it is reporting on.
    const store = makeStore({
      onMetric: () => {
        throw new Error("callback exploded");
      },
    });
    expect(() => store.metric("raises")).not.toThrow();
  });

  it("reports a throwing callback through the logger", () => {
    const warn = vi.fn();
    const store = makeStore({
      onMetric: () => {
        throw new Error("callback exploded");
      },
      logger: { ...noopLogger, warn },
    });
    store.metric("raises");
    expect(warn).toHaveBeenCalledOnce();
  });
});

describe("StateStore hijack-changed fan-out", () => {
  it("does nothing when no callback is configured", () => {
    expect(() => makeStore().notifyHijackChanged("w0", { enabled: true })).not.toThrow();
  });

  it("passes the worker, the flag and the owner", () => {
    const seen: Array<[string, boolean, string | undefined]> = [];
    const store = makeStore({
      onHijackChanged: (workerId, enabled, owner) => {
        seen.push([workerId, enabled, owner]);
      },
    });
    store.notifyHijackChanged("w1", { enabled: true, owner: "operator" });
    store.notifyHijackChanged("w2", { enabled: false });
    expect(seen).toStrictEqual(golden.notify.seen.slice(0, 2).map(([id, on, owner]) => [id, on, owner ?? undefined]));
  });

  it("ignores a non-promise return value", () => {
    // The types say void, but the hub is reachable from untyped JavaScript
    // and the reference asks isawaitable rather than "is not None" for
    // exactly this reason. Treating any return as a promise would throw.
    const store = makeStore({
      onHijackChanged: (() => 42) as unknown as NonNullable<StateStoreOptions["onHijackChanged"]>,
    });
    expect(() => store.notifyHijackChanged("w1", { enabled: true })).not.toThrow();
  });

  it("fires an async callback without waiting for it", async () => {
    // The caller is holding the hijack transition; it must not block on a
    // subscriber, and the reference fires the awaitable and forgets it.
    const seen: string[] = [];
    const store = makeStore({
      onHijackChanged: async (workerId) => {
        await Promise.resolve();
        seen.push(workerId);
      },
    });
    store.notifyHijackChanged("w3", { enabled: true, owner: "admin" });
    expect(golden.notify.async_callback_ran_before_yield).toBe(false);
    expect(seen).toStrictEqual([]);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(seen).toStrictEqual(["w3"]);
  });

  it("reports a rejecting async callback through the logger", async () => {
    const warn = vi.fn();
    const store = makeStore({
      onHijackChanged: () => Promise.reject(new Error("subscriber exploded")),
      logger: { ...noopLogger, warn },
    });
    store.notifyHijackChanged("w4", { enabled: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    // An unhandled rejection here would take down the process under Node's
    // default policy, so the fire-and-forget path has to attach a handler.
    expect(warn).toHaveBeenCalledOnce();
  });

  it("lets a synchronously throwing callback propagate", () => {
    // Deliberately unlike metric(), which swallows: the reference leaves this
    // call unguarded, so the exception reaches the caller. Swallowing it here
    // would silently diverge on a path that gates hijack transitions.
    expect(golden.notify.sync_raise_propagates).toBe(true);
    const store = makeStore({
      onHijackChanged: () => {
        throw new Error("subscriber exploded");
      },
    });
    expect(() => store.notifyHijackChanged("w5", { enabled: true })).toThrow("subscriber exploded");
  });
});
