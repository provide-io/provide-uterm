//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { FakeLeaseHub, FakeWorkerSocket, session } from "../testing/lease-harness.ts";
import { HijackLeaseManager, type HijackSession, WorkerRegistry, WorkerTermState } from "./index.ts";

interface SweepRecord {
  changed: boolean;
  calls: string[];
  owner_cleared: boolean;
  session_cleared: boolean;
}

interface RemoveRecord {
  notified: boolean;
  browsers_left: string[];
  owner_cleared: boolean;
  calls: string[];
}

interface HubLeaseLifecycleGolden {
  now: number;
  dashboard_lease_s: number;
  rest_lifecycle: {
    extended: number | null;
    wrong_id: number | null;
    wrong_owner: number | null;
    wrong_owner_metric: string[];
    unknown_worker: number | null;
    fresh: number;
    fresh_wrong_id: number;
    fresh_unknown: number;
    valid: boolean;
    valid_wrong_id: boolean;
    valid_unknown: boolean;
    valid_expired: boolean;
    released: [boolean, boolean];
    released_twice: [boolean, boolean];
    released_with_dashboard: [boolean, boolean];
  };
  cleanup: { unknown_worker: boolean } & Record<string, SweepRecord | boolean>;
  dead_browsers: { unknown_worker: boolean } & Record<string, RemoveRecord | boolean>;
  input: {
    open_mode: boolean;
    unknown_mode: boolean;
    unknown_prepare: boolean;
    still_hijacked: boolean;
    still_hijacked_unknown: boolean;
  } & Record<string, { allowed: boolean; owner_expires_at: number | null } | boolean>;
  events: {
    window_seqs: number[];
    window_latest_seq: number;
    window_min_event_seq: number;
    window_fresh_expires: number;
    all_seqs: number[];
    stale_fresh_expires: number;
  };
  get_session: {
    live_hijack_id: string | null;
    wrong_id_is_none: boolean;
    expired_is_none: boolean;
    expired_session_cleared: boolean;
    unknown_is_none: boolean;
  };
}

const golden = loadGolden<HubLeaseLifecycleGolden>("hub_lease_golden.json");
const NOW = golden.now;
const BROWSER = { id: "browser" };
const OTHER = { id: "other" };

/** A manager over one worker in the shape the case needs. */
function build(shape?: {
  owner?: object;
  ownerExpiresAt?: number;
  session?: HijackSession;
  inputMode?: "hijack" | "open";
}) {
  const registry = new WorkerRegistry<WorkerTermState>();
  const hub = new FakeLeaseHub();
  hub.now = () => NOW;
  const manager = new HijackLeaseManager({
    registry,
    hub,
    dashboardLeaseSeconds: golden.dashboard_lease_s,
    now: () => NOW,
    wallNow: () => NOW,
  });
  let state: WorkerTermState | undefined;
  if (shape !== undefined) {
    state = new WorkerTermState({ now: () => NOW });
    state.workerWs = new FakeWorkerSocket();
    state.inputMode = shape.inputMode ?? "hijack";
    state.hijackOwner = shape.owner;
    state.hijackOwnerExpiresAt = shape.ownerExpiresAt;
    state.hijackSession = shape.session;
    registry.put("w1", state);
  }
  return { manager, registry, hub, state };
}

describe("REST lease lifecycle", () => {
  const expected = golden.rest_lifecycle;

  it("extends a lease for the owner that holds it", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    expect(await manager.extendLease("w1", "h1", "operator", 90, NOW)).toBe(expected.extended);
  });

  it("refuses a heartbeat quoting the wrong hijack id", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    expect(await manager.extendLease("w1", "nope", "operator", 90, NOW)).toBe(expected.wrong_id ?? undefined);
  });

  it("refuses a heartbeat from a different owner", async () => {
    // Knowing the hijack id is not enough: an impostor holding a leaked id
    // must not be able to keep someone else's lease alive.
    const { manager, hub } = build({ session: session(NOW + 10) });
    expect(await manager.extendLease("w1", "h1", "impostor", 90, NOW)).toBe(expected.wrong_owner ?? undefined);
    expect(hub.calls.filter((call) => call.call === "metric").map((call) => call.name)).toStrictEqual(
      expected.wrong_owner_metric,
    );
  });

  it("leaves the lease untouched when a heartbeat is refused", async () => {
    const { manager, state } = build({ session: session(NOW + 10) });
    await manager.extendLease("w1", "h1", "impostor", 90, NOW);
    expect(state?.hijackSession?.leaseExpiresAt).toBe(NOW + 10);
  });

  it("refuses a heartbeat for an unknown worker", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    expect(await manager.extendLease("nope", "h1", "operator", 90, NOW)).toBe(expected.unknown_worker ?? undefined);
  });

  it("re-reads the expiry a heartbeat just extended", async () => {
    // The point of the re-read: the caller may have extended the lease since
    // it last looked, and must see the new expiry rather than its own stale
    // copy. Recorded against a lease that has just been heartbeaten.
    const { manager } = build({ session: session(NOW + 10) });
    await manager.extendLease("w1", "h1", "operator", 90, NOW);
    expect(await manager.getFreshExpiry("w1", "h1", 0)).toBe(expected.fresh);
  });

  it("falls back when the hijack id no longer matches", async () => {
    // The caller passes what it last knew; a lease that has been replaced
    // must not report the new holder's expiry to the old one.
    const { manager } = build({ session: session(NOW + 10) });
    expect(await manager.getFreshExpiry("w1", "nope", -1)).toBe(expected.fresh_wrong_id);
    expect(await manager.getFreshExpiry("nope", "h1", -2)).toBe(expected.fresh_unknown);
  });

  it("reports validity", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    expect(await manager.checkValid("w1", "h1")).toBe(expected.valid);
    expect(await manager.checkValid("w1", "nope")).toBe(expected.valid_wrong_id);
    expect(await manager.checkValid("nope", "h1")).toBe(expected.valid_unknown);
  });

  it("reports an expired session as invalid even though it is present", async () => {
    const { manager } = build({ session: session(NOW - 10) });
    expect(await manager.checkValid("w1", "h1")).toBe(expected.valid_expired);
  });

  it("releases and asks for a resume", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    const [ok, shouldResume] = expected.released;
    expect(await manager.releaseRest("w1", "h1")).toStrictEqual({ ok, shouldResume });
  });

  it("refuses a second release", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    await manager.releaseRest("w1", "h1");
    const [ok, shouldResume] = expected.released_twice;
    expect(await manager.releaseRest("w1", "h1")).toStrictEqual({ ok, shouldResume });
  });

  it("does not ask for a resume while a dashboard lease is held", async () => {
    // Resuming here would hand the worker back while a browser still holds it.
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 10, session: session(NOW + 10) });
    const [ok, shouldResume] = expected.released_with_dashboard;
    expect(await manager.releaseRest("w1", "h1")).toStrictEqual({ ok, shouldResume });
  });
});

describe("getRestSession", () => {
  const expected = golden.get_session;

  it("returns the live session", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    expect((await manager.getRestSession("w1", "h1"))?.hijackId).toBe(expected.live_hijack_id);
  });

  it("returns nothing for a mismatched id", async () => {
    const { manager } = build({ session: session(NOW + 10) });
    expect(await manager.getRestSession("w1", "other")).toBeUndefined();
  });

  it("cleans up first, so an expired session reads as absent", async () => {
    // The lookup runs the expiry sweep, so a caller cannot act on a lease
    // that has lapsed but not yet been swept.
    const { manager, registry } = build({ session: session(NOW - 10) });
    expect(await manager.getRestSession("w1", "h1")).toBeUndefined();
    expect(registry.require("w1").hijackSession === undefined).toBe(expected.expired_session_cleared);
  });

  it("returns nothing for an unknown worker", async () => {
    const { manager } = build();
    expect(await manager.getRestSession("nope", "h1")).toBeUndefined();
  });
});

describe("cleanupExpired", () => {
  /** Assert one recorded sweep. */
  async function sweep(key: string, shape: Parameters<typeof build>[0]): Promise<void> {
    const expected = golden.cleanup[key] as SweepRecord;
    const { manager, registry, hub } = build(shape);
    expect(await manager.cleanupExpired("w1")).toBe(expected.changed);
    // The call order is the contract: the resume recheck happens before the
    // events are appended and before anyone is told the state changed.
    expect(hub.callNames()).toStrictEqual(expected.calls);
    const after = registry.require("w1");
    expect(after.hijackOwner === undefined).toBe(expected.owner_cleared);
    expect(after.hijackSession === undefined).toBe(expected.session_cleared);
  }

  it("does nothing for an unknown worker", async () => {
    const { manager } = build();
    expect(await manager.cleanupExpired("nope")).toBe(golden.cleanup.unknown_worker);
  });

  it("does nothing when no lease is held", async () => {
    await sweep("idle", {});
  });

  it("does nothing when nothing has expired", async () => {
    await sweep("nothing_expired", { owner: BROWSER, ownerExpiresAt: NOW + 10, session: session(NOW + 10) });
  });

  it("expires a dashboard lease", async () => {
    await sweep("dashboard_expired", { owner: BROWSER, ownerExpiresAt: NOW - 10 });
  });

  it("expires a REST lease", async () => {
    await sweep("rest_expired", { session: session(NOW - 10) });
  });

  it("expires both and appends an event for each", async () => {
    await sweep("both_expired", { owner: BROWSER, ownerExpiresAt: NOW - 10, session: session(NOW - 10) });
  });

  it("does not ask for a resume while the other lease is still live", async () => {
    // Only a sweep that leaves the worker fully idle may resume it.
    await sweep("dashboard_expired_rest_live", {
      owner: BROWSER,
      ownerExpiresAt: NOW - 10,
      session: session(NOW + 10),
    });
    expect((golden.cleanup.dashboard_expired_rest_live as SweepRecord).calls).not.toContain("recheck_and_resume");
  });
});

describe("recheckAndResume", () => {
  it("resumes the worker when nothing took the lease back", async () => {
    const { manager, hub } = build({});
    await manager.recheckAndResume("w1", NOW);
    expect(hub.callNames()).toStrictEqual(["send_worker", "notify_hijack_changed"]);
    expect(hub.sent[0]).toMatchObject({ type: "control", action: "resume", owner: "lease-expired", lease_s: 0 });
  });

  it("stays quiet when a concurrent acquire took the worker", async () => {
    // Between the sweep and this check, another client can acquire; resuming
    // then would drop input from the new holder.
    const { manager, hub } = build({});
    hub.hijackedOverride = true;
    await manager.recheckAndResume("w1", NOW);
    expect(hub.callNames()).toStrictEqual([]);
  });

  it("resumes when the worker itself has gone", async () => {
    const { manager, hub } = build();
    await manager.recheckAndResume("nope", NOW);
    expect(hub.callNames()).toStrictEqual(["send_worker", "notify_hijack_changed"]);
  });
});

describe("removeDeadBrowsers", () => {
  /** Assert one recorded dead-socket removal. */
  async function remove(key: string, shape: Parameters<typeof build>[0], dead: object[]): Promise<void> {
    const expected = golden.dead_browsers[key] as RemoveRecord;
    const { manager, registry, hub, state } = build(shape);
    state?.browsers.set(BROWSER, "operator");
    state?.browsers.set(OTHER, "viewer");
    expect(await manager.removeDeadBrowsers("w1", new Set(dead))).toBe(expected.notified);
    const after = registry.require("w1");
    expect([...after.browsers.keys()].map((ws) => (ws as { id: string }).id).sort()).toStrictEqual(
      expected.browsers_left,
    );
    expect(after.hijackOwner === undefined).toBe(expected.owner_cleared);
    expect(hub.callNames()).toStrictEqual(expected.calls);
  }

  it("does nothing for an unknown worker", async () => {
    const { manager } = build();
    expect(await manager.removeDeadBrowsers("nope", new Set([BROWSER]))).toBe(golden.dead_browsers.unknown_worker);
  });

  it("drops a dead viewer without touching the hijack", async () => {
    await remove("non_owner_died", { owner: BROWSER, ownerExpiresAt: NOW + 10 }, [OTHER]);
  });

  it("resumes the worker when the holder's socket died", async () => {
    // Nobody is left to release the lease, so the worker would stay paused
    // forever if this did not resume it.
    await remove("owner_died", { owner: BROWSER, ownerExpiresAt: NOW + 10 }, [BROWSER]);
  });

  it("does not resume when a REST lease is still held", async () => {
    await remove("owner_died_rest_live", { owner: BROWSER, ownerExpiresAt: NOW + 10, session: session(NOW + 10) }, [
      BROWSER,
    ]);
  });

  it("does not clear an already-expired hold", async () => {
    await remove("owner_died_lease_expired", { owner: BROWSER, ownerExpiresAt: NOW - 10 }, [BROWSER]);
  });

  it("stays quiet when a concurrent acquire took the worker", async () => {
    const { manager, hub, state } = build({ owner: BROWSER, ownerExpiresAt: NOW + 10 });
    state?.browsers.set(BROWSER, "operator");
    hub.hijackedOverride = true;
    expect(await manager.removeDeadBrowsers("w1", new Set([BROWSER]))).toBe(false);
    expect(hub.callNames()).toStrictEqual([]);
  });
});

describe("input gating", () => {
  /** The recorded outcome of one prepareBrowserInput case. */
  function prepared(key: string): { allowed: boolean; owner_expires_at: number | null } {
    return golden.input[key] as { allowed: boolean; owner_expires_at: number | null };
  }

  it("reports open input mode", async () => {
    const { manager } = build({ inputMode: "open" });
    expect(await manager.isInputOpenMode("w1")).toBe(golden.input.open_mode);
    expect(await manager.isInputOpenMode("nope")).toBe(golden.input.unknown_mode);
  });

  it("reports whether any hijack is held", async () => {
    const { manager } = build({ owner: BROWSER, ownerExpiresAt: NOW + 10 });
    expect(await manager.stillHijacked("w1")).toBe(golden.input.still_hijacked);
    expect(await manager.stillHijacked("nope")).toBe(golden.input.still_hijacked_unknown);
  });

  it("refuses input for an unknown worker", async () => {
    const { manager } = build();
    expect(await manager.prepareBrowserInput("nope", BROWSER)).toBe(golden.input.unknown_prepare);
  });

  it("allows the hijack holder and extends its lease", async () => {
    // The extension rides on the input itself, so an operator who is actively
    // typing never has to heartbeat separately.
    const { manager, registry } = build({ owner: BROWSER, ownerExpiresAt: NOW + 1 });
    expect(await manager.prepareBrowserInput("w1", BROWSER)).toBe(prepared("hijack_mode_owner").allowed);
    expect(registry.require("w1").hijackOwnerExpiresAt).toBe(prepared("hijack_mode_owner").owner_expires_at);
  });

  it("refuses a non-holder and does not extend the holder's lease", async () => {
    const { manager, registry } = build({ owner: OTHER, ownerExpiresAt: NOW + 1 });
    expect(await manager.prepareBrowserInput("w1", BROWSER)).toBe(prepared("hijack_mode_not_owner").allowed);
    expect(registry.require("w1").hijackOwnerExpiresAt).toBe(prepared("hijack_mode_not_owner").owner_expires_at);
  });

  it.each([
    ["open_mode_viewer", "viewer"],
    ["open_mode_operator", "operator"],
    ["open_mode_admin", "admin"],
  ] as const)("gates %s by role in open mode", async (key, role) => {
    const { manager, state } = build({ inputMode: "open" });
    state?.browsers.set(BROWSER, role);
    expect(await manager.prepareBrowserInput("w1", BROWSER)).toBe(prepared(key).allowed);
  });

  it("treats an unknown browser as a viewer in open mode", async () => {
    const { manager } = build({ inputMode: "open" });
    expect(await manager.prepareBrowserInput("w1", BROWSER)).toBe(prepared("open_mode_unknown_role").allowed);
  });
});

describe("getEventsData", () => {
  /** A worker with a seven-event log. */
  function withEvents() {
    const built = build({ session: session(NOW + 10) });
    for (let seq = 1; seq <= 7; seq += 1) {
      built.state?.events.push({ seq, type: "output" });
    }
    if (built.state !== undefined) {
      built.state.eventSeq = 7;
      built.state.minEventSeq = 1;
    }
    return built;
  }

  it("filters by sequence before applying the limit", async () => {
    // Order matters: limiting first would return rows the caller has already
    // seen and silently drop the ones it has not.
    const { manager } = withEvents();
    const data = await manager.getEventsData("w1", "h1", session(NOW + 5), 2, 3);
    expect(data.rows.map((row) => row.seq)).toStrictEqual(golden.events.window_seqs);
    expect(data.latestSeq).toBe(golden.events.window_latest_seq);
    expect(data.minEventSeq).toBe(golden.events.window_min_event_seq);
    expect(data.freshExpires).toBe(golden.events.window_fresh_expires);
  });

  it("returns everything when the window is wide", async () => {
    const { manager } = withEvents();
    const data = await manager.getEventsData("w1", "h1", session(NOW + 5), 0, 100);
    expect(data.rows.map((row) => row.seq)).toStrictEqual(golden.events.all_seqs);
  });

  it("falls back to the caller's expiry when the hijack id no longer matches", async () => {
    const { manager } = withEvents();
    const data = await manager.getEventsData("w1", "other", session(NOW + 5), 0, 1);
    expect(data.freshExpires).toBe(golden.events.stale_fresh_expires);
  });

  it("treats an event with no sequence as the oldest possible", async () => {
    // The log is a plain record bag; a malformed entry must not throw or be
    // silently promoted past the caller's cursor.
    const { manager, state } = withEvents();
    state?.events.push({ type: "output" });
    const data = await manager.getEventsData("w1", "h1", session(NOW + 5), 0, 100);
    expect(data.rows.map((row) => row.seq)).toStrictEqual(golden.events.all_seqs);
  });

  it("returns an empty window for an unknown worker", async () => {
    const { manager } = build();
    const fallback = session(NOW + 5);
    const data = await manager.getEventsData("nope", "h1", fallback, 0, 10);
    expect(data).toStrictEqual({
      rows: [],
      latestSeq: 0,
      minEventSeq: 0,
      freshExpires: fallback.leaseExpiresAt,
    });
  });
});
