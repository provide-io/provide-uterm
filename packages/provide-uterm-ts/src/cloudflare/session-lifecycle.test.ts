//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  buildHelloFrame,
  DEFAULT_RESUME_TTL_S,
  type LifecycleAction,
  lifecycleStateAfter,
  onSocketClose,
  onSocketError,
  onSocketOpen,
  type SocketCloseState,
  type SocketOpenState,
  type SocketRole,
} from "./index.ts";

interface RecordedCase {
  role?: SocketRole;
  deleted?: boolean;
  presence?: boolean;
  browser_role?: string;
  input_mode?: string;
  already_initialized?: boolean;
  resume_on?: boolean;
  has_snapshot?: boolean;
  held_hijack?: boolean;
  has_resume_token?: boolean;
}

interface Recorded {
  name: string;
  case: RecordedCase;
  actions: Array<Record<string, unknown>>;
  closed: Array<{ code: number; reason: string }>;
  lifecycle_state: string;
  kv: Array<Record<string, unknown>>;
}

interface LifecycleGolden {
  fixed_ts: number;
  resume_token: string;
  ws_id: string;
  worker_id: string;
  open: Recorded[];
  close: Recorded[];
  error: Recorded[];
}

const golden = loadGolden<LifecycleGolden>("sessionlifecycle_golden.json");

/** The snapshot the recorded runtime held when it had one. */
const SNAPSHOT = { type: "snapshot", screen: "the screen" };

function openState(record: RecordedCase): SocketOpenState {
  return {
    role: record.role ?? "browser",
    deleted: record.deleted ?? false,
    workerId: golden.worker_id,
    wsId: golden.ws_id,
    browserRole: record.browser_role ?? "viewer",
    inputMode: record.input_mode ?? "open",
    presence: record.presence ?? false,
    // The recorded runtime had no worker socket and no ushell attached.
    workerOnline: false,
    alreadyInitialized: record.already_initialized ?? false,
    resumeEnabled: record.resume_on ?? true,
    resumeToken: golden.resume_token,
    lastSnapshot: (record.has_snapshot ?? true) ? SNAPSHOT : undefined,
    now: golden.fixed_ts,
  };
}

function closeState(record: RecordedCase): SocketCloseState {
  return {
    role: record.role ?? "browser",
    deleted: record.deleted ?? false,
    workerId: golden.worker_id,
    wsId: golden.ws_id,
    presence: record.presence ?? false,
    heldHijack: record.held_hijack ?? false,
    resumeToken: (record.has_resume_token ?? false) ? golden.resume_token : undefined,
    now: golden.fixed_ts,
  };
}

/**
 * The recorded actions, in this port's shape.
 *
 * The recording runtime logged `update_kv` and `on_browser_connected` through
 * its own stubs, so the two lists line up field for field once a couple of
 * recorded-only details are dropped.
 */
function expected(record: Recorded): Array<Record<string, unknown>> {
  return record.actions;
}

/** This port's actions, as the recording would have written them. */
function produced(actions: LifecycleAction[]): Array<Record<string, unknown>> {
  return actions.map((action) => {
    if (action.kind === "close") {
      // The recording closed the socket rather than logging an action.
      return { kind: "close" };
    }
    return action as unknown as Record<string, unknown>;
  });
}

describe("a socket arriving", () => {
  it.each(golden.open)("$name", (record) => {
    const actions = onSocketOpen(openState(record.case));
    if (record.closed.length > 0) {
      // A deleted session closes the socket and does nothing else.
      expect(actions).toEqual([{ kind: "close", code: 1001, reason: "session deleted" }]);
      expect(record.closed).toEqual([{ code: 1001, reason: "session deleted" }]);
      expect(record.actions).toEqual([]);
      return;
    }
    // Compared whole, field for field, and not merely by the kinds in it:
    // `exclude_self`, a resume token's ttl and a replayed screen are all
    // decisions, and a comparison of names alone would let any of them drift.
    expect(produced(actions)).toEqual(expected(record));
  });

  it("tells a browser it may hijack only when it is an admin", () => {
    // The resolved role decides it, not the one the browser asked for: a port
    // that read the request would put hijack controls in front of a viewer.
    for (const [role, canHijack] of [
      ["admin", true],
      ["operator", false],
      ["viewer", false],
      ["superuser", false],
      ["", false],
      ["Admin", false],
    ] as const) {
      expect(buildHelloFrame(openState({ browser_role: role })).can_hijack).toBe(canHijack);
    }
  });

  it("matches the hello the reference sent, field for field", () => {
    const record = golden.open.find((entry) => entry.name === "an admin browser after hibernation") as Recorded;
    const hello = record.actions.find((action) => (action.frame as { type?: string } | undefined)?.type === "hello");
    expect(buildHelloFrame(openState(record.case))).toEqual(hello?.frame);
  });

  it("sends hello on a hibernation restore and not on a fresh upgrade", () => {
    // `fetch` already sent it before the 101 on a normal upgrade; on a restore
    // it never ran. Either mistake means two hellos or none.
    const restored = onSocketOpen(openState({ browser_role: "admin", already_initialized: false }));
    const fresh = onSocketOpen(openState({ browser_role: "admin", already_initialized: true }));
    expect(restored.filter((action) => action.kind === "send").length).toBe(2);
    expect(fresh.filter((action) => action.kind === "send").length).toBe(1);
    expect(fresh.some((action) => action.kind === "create_resume_token")).toBe(false);
  });

  it("gives a resume token the lifetime it was configured with", () => {
    const explicit = onSocketOpen({ ...openState({ browser_role: "admin" }), resumeTtlS: 42 });
    expect(explicit).toContainEqual({
      kind: "create_resume_token",
      token: golden.resume_token,
      worker_id: golden.worker_id,
      role: "admin",
      ttl: 42,
    });
    // And the reference's own default when nobody configured one.
    const recorded = golden.open.find((entry) => entry.name === "an admin browser after hibernation") as Recorded;
    const recordedTtl = recorded.actions.find((action) => action.kind === "create_resume_token")?.ttl;
    expect(DEFAULT_RESUME_TTL_S).toBe(recordedTtl);
  });

  it("mints a resume token only when resume is on", () => {
    const on = onSocketOpen(openState({ browser_role: "admin", resume_on: true }));
    const off = onSocketOpen(openState({ browser_role: "admin", resume_on: false }));
    expect(on.some((action) => action.kind === "create_resume_token")).toBe(true);
    expect(off.some((action) => action.kind === "create_resume_token")).toBe(false);
    expect(buildHelloFrame(openState({ resume_on: false })).resume_token).toBeUndefined();
    expect(buildHelloFrame(openState({ resume_on: true })).resume_token).toBe(golden.resume_token);
  });

  it("replays the screen to a browser and the text to a raw socket", () => {
    // A raw socket is a plain terminal: it gets characters, not frames.
    const browser = onSocketOpen(openState({ role: "browser", browser_role: "admin" }));
    expect(browser.filter((action) => action.kind === "send").at(-1)).toEqual({ kind: "send", frame: SNAPSHOT });
    const raw = onSocketOpen(openState({ role: "raw" }));
    expect(raw).toEqual([
      { kind: "register_socket", role: "raw" },
      { kind: "send_text", text: "the screen" },
    ]);
  });

  it("says nothing to a raw socket whose screen is not text", () => {
    // A snapshot can carry anything; only a string is something to write to a
    // terminal.
    expect(onSocketOpen({ ...openState({ role: "raw" }), lastSnapshot: { type: "snapshot", screen: 123 } })).toEqual([
      { kind: "register_socket", role: "raw" },
    ]);
  });

  it("says nothing to a raw socket with no screen yet", () => {
    expect(onSocketOpen(openState({ role: "raw", has_snapshot: false }))).toEqual([
      { kind: "register_socket", role: "raw" },
    ]);
  });

  it("announces a worker and marks it connected", () => {
    expect(onSocketOpen(openState({ role: "worker" }))).toEqual([
      { kind: "register_socket", role: "worker" },
      {
        kind: "broadcast_worker_frame",
        frame: { type: "worker_connected", worker_id: golden.worker_id, ts: golden.fixed_ts },
      },
      { kind: "update_kv", connected: true },
    ]);
  });

  it("closes a socket arriving at a deleted session", () => {
    // There is nobody left to tell anything to.
    for (const role of ["worker", "browser", "raw"] as const) {
      expect(onSocketOpen(openState({ role, deleted: true }))).toEqual([
        { kind: "close", code: 1001, reason: "session deleted" },
      ]);
    }
  });
});

describe("a socket going", () => {
  it.each(golden.close)("$name, closing", (record) => {
    expect(produced(onSocketClose(closeState(record.case)))).toEqual(expected(record));
  });

  it.each(golden.error)("$name, failing", (record) => {
    expect(produced(onSocketError(closeState(record.case)))).toEqual(expected(record));
  });

  it("lets a browser that was driving reclaim the hijack", () => {
    // Marked before the socket goes, so a reconnect can take ownership back.
    const actions = onSocketClose(closeState({ held_hijack: true, has_resume_token: true }));
    expect(actions).toContainEqual({
      kind: "mark_resume_hijack_owner",
      token: golden.resume_token,
      owner: true,
    });
  });

  it("cannot mark what it was never given", () => {
    for (const token of [undefined, ""]) {
      const actions = onSocketClose({ ...closeState({ held_hijack: true }), resumeToken: token });
      expect(actions.some((action) => action.kind === "mark_resume_hijack_owner")).toBe(false);
    }
  });

  it("does not mark a browser that was not driving", () => {
    const actions = onSocketClose(closeState({ held_hijack: false, has_resume_token: true }));
    expect(actions.some((action) => action.kind === "mark_resume_hijack_owner")).toBe(false);
  });

  it("tells the others somebody left, when the session tracks presence", () => {
    expect(onSocketClose(closeState({ presence: true }))).toContainEqual({
      kind: "broadcast_browsers",
      frame: { type: "presence_leave", user_id: golden.ws_id, ts: golden.fixed_ts },
    });
    expect(onSocketClose(closeState({ presence: false })).some((action) => action.kind === "broadcast_browsers")).toBe(
      false,
    );
  });

  it("says nothing about a deleted session but still lets the socket go", () => {
    const actions = onSocketClose(closeState({ presence: true, deleted: true, held_hijack: true }));
    expect(actions).toEqual([{ kind: "remove_socket" }]);
  });

  it("still records a worker as disconnected on a deleted session", () => {
    // Nobody is told, but the registry is corrected — an entry left saying
    // `connected` would outlive the session.
    expect(onSocketClose(closeState({ role: "worker", deleted: true }))).toEqual([
      { kind: "remove_socket" },
      { kind: "update_kv", connected: false },
    ]);
  });

  it("leaves a session stopped on a close and in error on a failure", () => {
    // The one place the two handlers differ.
    expect(lifecycleStateAfter("close", { role: "worker", deleted: false })).toBe("stopped");
    expect(lifecycleStateAfter("error", { role: "worker", deleted: false })).toBe("error");
    expect(lifecycleStateAfter("open", { role: "worker", deleted: false })).toBe("running");
  });

  it("moves nothing for a browser or a raw socket", () => {
    for (const role of ["browser", "raw"] as const) {
      for (const event of ["open", "close", "error"] as const) {
        expect(lifecycleStateAfter(event, { role, deleted: false })).toBeUndefined();
      }
    }
  });

  it("moves nothing on a deleted session", () => {
    for (const event of ["open", "close", "error"] as const) {
      expect(lifecycleStateAfter(event, { role: "worker", deleted: true })).toBeUndefined();
    }
  });

  it("matches the states the reference left behind", () => {
    for (const record of golden.close) {
      const state = lifecycleStateAfter("close", {
        role: record.case.role ?? "browser",
        deleted: record.case.deleted ?? false,
      });
      // The recorded runtime started at `starting`, so an untouched state
      // means this port should have moved nothing.
      expect(state ?? "starting").toBe(record.lifecycle_state);
    }
    for (const record of golden.error) {
      const state = lifecycleStateAfter("error", {
        role: record.case.role ?? "browser",
        deleted: record.case.deleted ?? false,
      });
      expect(state ?? "starting").toBe(record.lifecycle_state);
    }
  });
});
