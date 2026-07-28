//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type BrowserRole,
  type PresenceHubCallbacks,
  PresenceManager,
  WorkerRegistry,
  WorkerTermState,
} from "./index.ts";

interface HubPresenceGolden {
  now: number;
  input: Array<{
    name: string;
    input_mode: "hijack" | "open";
    role: BrowserRole | null;
    is_owner: boolean;
    allowed: boolean;
  }>;
  snapshots: Array<{
    name: string;
    is_hijacked: boolean;
    hijacked_by_me: boolean;
    worker_online: boolean;
    input_mode: string;
  }>;
  control_frames: {
    types: string[];
    timestamps: number[];
    req_id_lengths: number[];
    req_ids_differ: boolean;
    keys: string[][];
  };
  roles: { resolved: BrowserRole[] };
}

const golden = loadGolden<HubPresenceGolden>("hub_presence_golden.json");
const NOW = golden.now;
const ASKER = { id: "asker" };
const OTHER = { id: "someone-else" };

/** A recording stand-in for the hub surface presence reaches through. */
class FakeHub implements PresenceHubCallbacks {
  readonly registry = new WorkerRegistry<WorkerTermState>();
  readonly sent: Array<Record<string, unknown>> = [];
  role: BrowserRole = "viewer";

  isDashboardHijackActive(state: WorkerTermState): boolean {
    if (state.hijackOwner === undefined) {
      return false;
    }
    return state.hijackOwnerExpiresAt === undefined || state.hijackOwnerExpiresAt > NOW;
  }

  hasValidRestLease(state: WorkerTermState): boolean {
    return state.hijackSession !== undefined && state.hijackSession.leaseExpiresAt > NOW;
  }

  isHijacked(state: WorkerTermState): boolean {
    return this.isDashboardHijackActive(state) || this.hasValidRestLease(state);
  }

  async resolveRoleForBrowser(): Promise<BrowserRole> {
    return this.role;
  }

  async sendWorker(_workerId: string, message: Record<string, unknown>): Promise<boolean> {
    this.sent.push(message);
    return true;
  }
}

/** A presence manager over a recording hub. */
function build() {
  const hub = new FakeHub();
  const presence = new PresenceManager({ hub, wallNow: () => NOW });
  return { hub, presence };
}

/** How each recorded snapshot case sets its worker up. */
const SNAPSHOT_SHAPES: Record<
  string,
  | {
      connected?: boolean;
      inputMode?: "hijack" | "open";
      owner?: object;
      ownerExpiresAt?: number;
      restExpiresAt?: number;
    }
  | undefined
> = {
  "unknown worker": undefined,
  "idle worker": {},
  "worker offline": { connected: false },
  "hijacked by me": { owner: ASKER, ownerExpiresAt: NOW + 10 },
  "hijacked by someone else": { owner: OTHER, ownerExpiresAt: NOW + 10 },
  "my hijack has expired": { owner: ASKER, ownerExpiresAt: NOW - 10 },
  "rest lease held": { restExpiresAt: NOW + 10 },
  "open input mode": { inputMode: "open" },
  "perpetual hold by me": { owner: ASKER },
  "my hold lapsed, rest took over": { owner: ASKER, ownerExpiresAt: NOW - 10, restExpiresAt: NOW + 10 },
};

describe("PresenceManager.canSendInput", () => {
  it.each(golden.input)("$name", (record) => {
    // This runs on every browser input frame. In hijack mode only the lease
    // holder may type; in open mode the lease is irrelevant and the role
    // decides, with viewers still refused.
    const { presence } = build();
    const state = new WorkerTermState({ now: () => NOW });
    state.inputMode = record.input_mode;
    state.hijackOwner = record.is_owner ? ASKER : OTHER;
    state.hijackOwnerExpiresAt = NOW + 10;
    if (record.role !== null) {
      state.browsers.set(ASKER, record.role);
    }
    expect(presence.canSendInput(state, ASKER)).toBe(record.allowed);
  });

  it("does not let the lease holder bypass the role check in open mode", () => {
    // Open mode is not "hijack mode plus everyone else" — holding the lease
    // stops meaning anything, so a holder with no role is a viewer.
    const record = golden.input.find((entry) => entry.name === "open mode, holder with no role");
    expect(record?.is_owner).toBe(true);
    expect(record?.allowed).toBe(false);
  });

  it("treats a browser it has never seen as a viewer", () => {
    const record = golden.input.find((entry) => entry.name === "open mode, unknown browser");
    expect(record?.allowed).toBe(false);
  });
});

describe("PresenceManager.registerBrowserStateSnapshot", () => {
  it.each(golden.snapshots)("$name", async (record) => {
    const { hub, presence } = build();
    const shape = SNAPSHOT_SHAPES[record.name];
    if (shape !== undefined) {
      const state = new WorkerTermState({ now: () => NOW });
      state.workerWs = shape.connected === false ? undefined : { sendText: async () => {} };
      state.inputMode = shape.inputMode ?? "hijack";
      state.hijackOwner = shape.owner;
      state.hijackOwnerExpiresAt = shape.ownerExpiresAt;
      if (shape.restExpiresAt !== undefined) {
        state.hijackSession = { hijackId: "h1", owner: "cli", leaseExpiresAt: shape.restExpiresAt };
      }
      hub.registry.put("w1", state);
    }
    expect(await presence.registerBrowserStateSnapshot("w1", ASKER)).toStrictEqual({
      isHijacked: record.is_hijacked,
      hijackedByMe: record.hijacked_by_me,
      workerOnline: record.worker_online,
      inputMode: record.input_mode,
    });
  });

  it("answers for a worker it does not know rather than failing", async () => {
    // A browser can attach before its worker connects; an error here would
    // break the page instead of showing an empty session.
    const record = golden.snapshots.find((entry) => entry.name === "unknown worker");
    expect(record).toStrictEqual({
      name: "unknown worker",
      is_hijacked: false,
      hijacked_by_me: false,
      worker_online: false,
      input_mode: "hijack",
    });
  });

  it("does not claim a hold that has lapsed", async () => {
    const record = golden.snapshots.find((entry) => entry.name === "my hijack has expired");
    expect(record?.hijacked_by_me).toBe(false);
  });

  it("does not claim someone else's session because mine used to be here", async () => {
    // My hold lapsed and a REST client took the worker. The session is
    // hijacked and my identity is still in the owner slot, so asking the
    // broader "is anything holding this?" question here would tell the UI I
    // am still in control while someone else types.
    const record = golden.snapshots.find((entry) => entry.name === "my hold lapsed, rest took over");
    expect(record?.is_hijacked).toBe(true);
    expect(record?.hijacked_by_me).toBe(false);
  });
});

describe("PresenceManager worker pokes", () => {
  it("sends a snapshot request", async () => {
    const { hub, presence } = build();
    await presence.requestSnapshot("w1");
    expect(hub.sent).toHaveLength(1);
    expect(hub.sent[0]?.type).toBe(golden.control_frames.types[0]);
    expect(hub.sent[0]?.ts).toBe(golden.control_frames.timestamps[0]);
    expect(Object.keys(hub.sent[0] ?? {}).sort()).toStrictEqual(golden.control_frames.keys[0]);
  });

  it("sends an analysis request", async () => {
    const { hub, presence } = build();
    await presence.requestAnalysis("w1");
    expect(hub.sent[0]?.type).toBe(golden.control_frames.types[1]);
  });

  it("gives each request its own id", async () => {
    // The worker correlates its reply by this id, so two outstanding pokes
    // sharing one would have their answers confused.
    expect(golden.control_frames.req_ids_differ).toBe(true);
    const { hub, presence } = build();
    await presence.requestSnapshot("w1");
    await presence.requestSnapshot("w1");
    expect(hub.sent[0]?.req_id).not.toBe(hub.sent[1]?.req_id);
    expect(String(hub.sent[0]?.req_id).length).toBe(golden.control_frames.req_id_lengths[0]);
  });

  it("stamps a poke with wall time by default", async () => {
    const hub = new FakeHub();
    const presence = new PresenceManager({ hub });
    await presence.requestSnapshot("w1");
    expect(Math.abs(Number(hub.sent[0]?.ts) - Date.now() / 1000)).toBeLessThan(5);
  });
});

describe("PresenceManager.resolveRoleForBrowser", () => {
  it.each(golden.roles.resolved)("passes %s through from the hub's resolver", async (role) => {
    const { hub, presence } = build();
    hub.role = role;
    expect(await presence.resolveRoleForBrowser(ASKER, "w1")).toBe(role);
  });
});
