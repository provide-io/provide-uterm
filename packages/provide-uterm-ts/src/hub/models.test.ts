//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  BoundedDeque,
  EVENT_DEQUE_MAXLEN,
  HijackLease,
  type HijackSession,
  VALID_ROLES,
  WorkerTermState,
} from "./index.ts";

interface HubModelsGolden {
  now: number;
  leases: Array<{
    name: string;
    ws_present: boolean;
    ws_expires_at: number | null;
    session_expires_at: number | null;
    is_idle: boolean;
    is_dashboard_active: boolean;
    is_rest_active: boolean;
    is_active: boolean;
    rest_expired: boolean;
    dash_expired: boolean;
    ws_after: string | null;
    ws_expires_at_after: number | null;
    session_after: number | null;
    is_idle_after: boolean;
  }>;
  state: {
    defaults: {
      worker_ws_is_none: boolean;
      browsers: Record<string, string>;
      hijack_owner_is_none: boolean;
      hijack_owner_expires_at: number | null;
      hijack_session_is_none: boolean;
      hijack_pending: string | null;
      input_mode: string;
      last_snapshot_is_none: boolean;
      events: unknown[];
      events_maxlen: number;
      event_seq: number;
      min_event_seq: number;
      protocol_version: number | null;
      is_tunnel_worker: boolean;
      graphical_session_is_none: boolean;
    };
    view_mutation_leaked: boolean;
    view_is_same_object: boolean;
    applied_ws: string;
    applied_expires_at: number;
    applied_session_is_none: boolean;
    valid_roles: string[];
    activity: {
      defaults_to_zero: boolean;
      is_monotonic_now: boolean;
      does_not_go_backwards: boolean;
    };
  };
  events: {
    len_after_overflow: number;
    first_seq: number;
    last_seq: number;
    rebuilt_len: number;
    rebuilt_seqs: number[];
  };
}

const golden = loadGolden<HubModelsGolden>("hub_models_golden.json");
const NOW = golden.now;

/** A stand-in for a browser WebSocket, which the hub compares by identity. */
const WS = { id: "ws" };

/** Build a lease from the three slots the recorded cases vary. */
function lease(wsPresent: boolean, wsExpiresAt: number | null, sessionExpiresAt: number | null): HijackLease {
  const session: HijackSession | undefined =
    sessionExpiresAt === null
      ? undefined
      : { hijackId: "h1", owner: "operator", leaseExpiresAt: sessionExpiresAt, acquiredAt: 0, lastHeartbeat: 0 };
  return new HijackLease({
    ws: wsPresent ? WS : undefined,
    wsExpiresAt: wsExpiresAt ?? undefined,
    session,
  });
}

describe("HijackLease predicates", () => {
  it.each(golden.leases)("$name", (record) => {
    // The two boundaries are deliberately different in the reference: a lease
    // expiring at exactly now is inactive (`> now`) *and* expired (`<= now`).
    const subject = lease(record.ws_present, record.ws_expires_at, record.session_expires_at);
    expect(subject.isIdle).toBe(record.is_idle);
    expect(subject.isDashboardActive(NOW)).toBe(record.is_dashboard_active);
    expect(subject.isRestActive(NOW)).toBe(record.is_rest_active);
    expect(subject.isActive(NOW)).toBe(record.is_active);
  });
});

describe("HijackLease expiry", () => {
  it.each(golden.leases)("$name", (record) => {
    const subject = lease(record.ws_present, record.ws_expires_at, record.session_expires_at);
    expect(subject.expire(NOW)).toStrictEqual({
      restExpired: record.rest_expired,
      dashExpired: record.dash_expired,
    });
    expect(subject.ws).toBe(record.ws_after === null ? undefined : WS);
    expect(subject.wsExpiresAt).toBe(record.ws_expires_at_after ?? undefined);
    expect(subject.session?.leaseExpiresAt).toBe(record.session_after ?? undefined);
    expect(subject.isIdle).toBe(record.is_idle_after);
  });

  it("never clears a dashboard slot that carries no expiry", () => {
    // A quirk of the reference worth pinning: an occupied ws slot with no
    // wsExpiresAt is neither active nor expirable, so it is never reclaimed
    // here — releasing it is the connection lifecycle's job.
    const subject = lease(true, null, null);
    expect(subject.expire(NOW)).toStrictEqual({ restExpired: false, dashExpired: false });
    expect(subject.ws).toBe(WS);
    expect(subject.isActive(NOW)).toBe(false);
  });
});

describe("WorkerTermState defaults", () => {
  const defaults = golden.state.defaults;

  it("starts with no connections and hijack input mode", () => {
    const state = new WorkerTermState();
    expect(state.workerWs).toBeUndefined();
    expect(state.browsers.size).toBe(Object.keys(defaults.browsers).length);
    expect(state.hijackOwner).toBeUndefined();
    expect(state.hijackOwnerExpiresAt).toBeUndefined();
    expect(state.hijackSession).toBeUndefined();
    expect(state.hijackPending).toBeUndefined();
    expect(state.inputMode).toBe(defaults.input_mode);
  });

  it("starts with an empty event log and zeroed sequence counters", () => {
    const state = new WorkerTermState();
    expect(state.lastSnapshot).toBeUndefined();
    expect(state.events.toArray()).toStrictEqual(defaults.events);
    expect(state.events.maxlen).toBe(defaults.events_maxlen);
    expect(EVENT_DEQUE_MAXLEN).toBe(defaults.events_maxlen);
    expect(state.eventSeq).toBe(defaults.event_seq);
    expect(state.minEventSeq).toBe(defaults.min_event_seq);
  });

  it("starts unnegotiated, untunnelled and with no graphical session", () => {
    const state = new WorkerTermState();
    expect(state.protocolVersion).toBeUndefined();
    expect(state.isTunnelWorker).toBe(defaults.is_tunnel_worker);
    expect(state.graphicalSession).toBeUndefined();
  });

  it("gives each state its own browser map and event log", () => {
    // A shared default would let one worker's browsers appear on another.
    const first = new WorkerTermState();
    const second = new WorkerTermState();
    first.browsers.set(WS, "admin");
    first.events.push({ seq: 1 });
    expect(second.browsers.size).toBe(0);
    expect(second.events.toArray()).toStrictEqual([]);
  });

  it("recognises the reference roles", () => {
    expect([...VALID_ROLES].sort()).toStrictEqual(golden.state.valid_roles);
  });

  it("seeds last activity from the clock rather than from zero", () => {
    // A state created and never touched must not read as infinitely idle to
    // the pruner, so the reference seeds this with the monotonic clock.
    const activity = golden.state.activity;
    expect(activity.defaults_to_zero).toBe(false);
    expect(new WorkerTermState().lastActivityAt).toBeGreaterThan(0);
  });

  it("does not go backwards between two states", () => {
    expect(golden.state.activity.does_not_go_backwards).toBe(true);
    const first = new WorkerTermState();
    const second = new WorkerTermState();
    expect(second.lastActivityAt).toBeGreaterThanOrEqual(first.lastActivityAt);
  });

  it("takes an injected clock", () => {
    expect(new WorkerTermState({ now: () => 42 }).lastActivityAt).toBe(42);
  });
});

describe("WorkerTermState lease view", () => {
  it("hands back a fresh view each time", () => {
    const state = new WorkerTermState();
    expect(golden.state.view_is_same_object).toBe(false);
    expect(state.lease).not.toBe(state.lease);
  });

  it("does not write mutations of the view back into the state", () => {
    // The view borrows the slots; only applyLease writes. Were it live, a
    // read-only predicate caller could release someone else's hijack.
    const state = new WorkerTermState();
    state.hijackOwner = WS;
    state.hijackOwnerExpiresAt = NOW + 10;
    const view = state.lease;
    view.ws = undefined;
    view.wsExpiresAt = undefined;
    expect(golden.state.view_mutation_leaked).toBe(false);
    expect(state.hijackOwner).toBe(WS);
    expect(state.hijackOwnerExpiresAt).toBe(NOW + 10);
  });

  it("reflects the state's current dashboard lease", () => {
    const state = new WorkerTermState();
    state.hijackOwner = WS;
    state.hijackOwnerExpiresAt = NOW + 10;
    expect(state.lease.ws).toBe(WS);
    expect(state.lease.isDashboardActive(NOW)).toBe(true);
  });

  it("reflects the state's current REST lease", () => {
    // The other half of the same slot: a view that carried only the ws
    // fields would report an idle lease while a REST client held the worker.
    const state = new WorkerTermState();
    const session: HijackSession = {
      hijackId: "h1",
      owner: "operator",
      leaseExpiresAt: NOW + 10,
    };
    state.hijackSession = session;
    expect(state.lease.session).toBe(session);
    expect(state.lease.isRestActive(NOW)).toBe(true);
    expect(state.lease.isIdle).toBe(false);
  });

  it("writes a view back through applyLease", () => {
    const state = new WorkerTermState();
    const other = { id: "ws2" };
    state.applyLease(new HijackLease({ ws: other, wsExpiresAt: golden.state.applied_expires_at }));
    expect(state.hijackOwner).toBe(other);
    expect(state.hijackOwnerExpiresAt).toBe(golden.state.applied_expires_at);
    expect(state.hijackSession).toBeUndefined();
  });

  it("writes a REST lease back through applyLease", () => {
    const state = new WorkerTermState();
    const session: HijackSession = { hijackId: "h1", owner: "cli", leaseExpiresAt: NOW + 10 };
    state.applyLease(new HijackLease({ session }));
    expect(state.hijackSession).toBe(session);
  });

  it("clears the state's fields when an emptied view is applied", () => {
    const state = new WorkerTermState();
    state.hijackOwner = WS;
    state.hijackOwnerExpiresAt = NOW + 10;
    const view = state.lease;
    view.expire(NOW + 20);
    state.applyLease(view);
    expect(state.hijackOwner).toBeUndefined();
    expect(state.hijackOwnerExpiresAt).toBeUndefined();
  });
});

describe("BoundedDeque", () => {
  it("drops from the front once it is full", () => {
    const state = new WorkerTermState();
    for (let seq = 0; seq < EVENT_DEQUE_MAXLEN + 3; seq += 1) {
      state.events.push({ seq });
    }
    expect(state.events.length).toBe(golden.events.len_after_overflow);
    expect(state.events.at(0)).toStrictEqual({ seq: golden.events.first_seq });
    expect(state.events.at(-1)).toStrictEqual({ seq: golden.events.last_seq });
  });

  it("keeps the newest entries when rebuilt with a smaller bound", () => {
    // The hub rebuilds this on worker connect with its configured maxlen,
    // and an over-long log must lose its oldest entries, not its newest.
    const state = new WorkerTermState();
    for (let seq = 0; seq < EVENT_DEQUE_MAXLEN + 3; seq += 1) {
      state.events.push({ seq });
    }
    const rebuilt = state.events.withMaxlen(golden.events.rebuilt_len);
    expect(rebuilt.length).toBe(golden.events.rebuilt_len);
    expect(rebuilt.toArray()).toStrictEqual(golden.events.rebuilt_seqs.map((seq) => ({ seq })));
    expect(rebuilt.maxlen).toBe(golden.events.rebuilt_len);
  });

  it("leaves a short log alone when rebuilt with a larger bound", () => {
    const deque = new BoundedDeque<number>(4);
    deque.push(1);
    deque.push(2);
    const rebuilt = deque.withMaxlen(10);
    expect(rebuilt.toArray()).toStrictEqual([1, 2]);
    expect(rebuilt.maxlen).toBe(10);
  });

  it("does not alias the deque it was rebuilt from", () => {
    const deque = new BoundedDeque<number>(4);
    deque.push(1);
    const rebuilt = deque.withMaxlen(4);
    rebuilt.push(2);
    expect(deque.toArray()).toStrictEqual([1]);
  });

  it("returns undefined for an index it does not hold", () => {
    const deque = new BoundedDeque<number>(4);
    expect(deque.at(0)).toBeUndefined();
    deque.push(1);
    expect(deque.at(5)).toBeUndefined();
    expect(deque.at(-2)).toBeUndefined();
  });

  it("returns a snapshot that a later push does not disturb", () => {
    const deque = new BoundedDeque<number>(4);
    deque.push(1);
    const snapshot = deque.toArray();
    deque.push(2);
    expect(snapshot).toStrictEqual([1]);
  });

  it("iterates oldest first", () => {
    const deque = new BoundedDeque<number>(2);
    deque.push(1);
    deque.push(2);
    deque.push(3);
    expect([...deque]).toStrictEqual([2, 3]);
  });

  it("refuses a bound below one", () => {
    // A zero-length log would silently discard every event it was handed.
    expect(() => new BoundedDeque<number>(0)).toThrow(RangeError);
  });
});
