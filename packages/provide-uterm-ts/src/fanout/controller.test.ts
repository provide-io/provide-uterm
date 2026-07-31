//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { PromptRegexError } from "../hub/index.ts";
import type { AuthorizablePrincipal } from "../server/authorization.ts";
import {
  FanOutController,
  type FanOutControllerHub,
  type FanOutGroup,
  fanOutGroup,
  InMemoryFanOutStore,
} from "./index.ts";

const NOW = 1000;

/** A hub that records what it was asked to do and can be made to refuse. */
class FakeHub implements FanOutControllerHub {
  readonly sent: Array<{ workerId: string; message: Record<string, unknown> }> = [];
  readonly broadcasts: Array<{ workerId: string; message: Record<string, unknown> }> = [];
  readonly events: Array<{ workerId: string; eventType: string; data?: Record<string, unknown> | undefined }> = [];
  /** Workers that refuse a send. */
  readonly refusing = new Set<string>();
  /** Workers whose collection throws. */
  readonly exploding = new Set<string>();
  /** What each worker prints. */
  readonly outputs = new Map<string, string>();
  /** Approval expiry subscriber, as the hub's approval store would set it. */
  onApprovalExpired: ((requestId: string) => void) | undefined;
  readonly approvals: Array<Record<string, unknown>> = [];

  async sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean> {
    this.sent.push({ workerId, message });
    return !this.refusing.has(workerId);
  }

  async broadcast(workerId: string, message: Record<string, unknown>): Promise<void> {
    this.broadcasts.push({ workerId, message });
  }

  async appendEvent(workerId: string, eventType: string, data?: Record<string, unknown>): Promise<void> {
    this.events.push({ workerId, eventType, data });
  }

  addApproval(request: Record<string, unknown>): void {
    this.approvals.push(request);
  }

  async collectOutput(
    workerId: string,
    _options: { quiesceMs: number; maxMs: number },
  ): Promise<{ output: string; elapsedMs: number }> {
    if (this.exploding.has(workerId)) {
      throw new Error(`collection failed for ${workerId}`);
    }
    return { output: this.outputs.get(workerId) ?? "", elapsedMs: 7 };
  }

  async openOutputCapture(workerId: string) {
    return {
      collect: async (options: { quiesceMs: number; maxMs: number }) => this.collectOutput(workerId, options),
      close: async () => {},
    };
  }
}

function actor(subjectId: string, roles: string[] = ["admin"]): AuthorizablePrincipal {
  return { subject_id: subjectId, roles: new Set(roles), scopes: new Set<string>() };
}

function security() {
  return {
    isGlobalAdmin: async (principal: AuthorizablePrincipal) => principal.roles.has("admin"),
    resolveSession: async (workerId: string) => ({ workerId }),
    canReadSession: async () => true,
  };
}

/** A controller over a recording hub, with ids and time pinned. */
function build(options: Partial<ConstructorParameters<typeof FanOutController>[0]> = {}) {
  const hub = new FakeHub();
  let counter = 0;
  const store = new InMemoryFanOutStore();
  const controller = new FanOutController({
    hub,
    store,
    now: () => NOW,
    newId: () => `id-${++counter}`,
    ...security(),
    ...options,
  });
  return { hub, store, controller };
}

/** A group of `workerIds` owned by alice. */
function group(workerIds: string[], overrides: Partial<FanOutGroup> = {}): FanOutGroup {
  return {
    ...fanOutGroup({
      groupId: "g1",
      name: "fleet",
      workerIds,
      createdBy: "alice",
      createdAt: NOW,
    }),
    ...overrides,
  };
}

describe("FanOutController group management", () => {
  it("stores a group and stamps its creator", async () => {
    // The caller supplies the record but not who owns it — that comes from
    // the authenticated principal, or anyone could create groups as anyone.
    const { controller, store } = build();
    const created = group(["w1"], { createdBy: "not-really-alice" });
    expect(await controller.createGroup(created, "alice")).toBe("g1");
    expect((await store.get("g1"))?.createdBy).toBe("alice");
  });

  it("refuses a group larger than the cap", async () => {
    // Every member is a session the hub drives on one keystroke; the cap is
    // what stops one request fanning out to the whole estate.
    const { controller } = build({ maxGroupSize: 2 });
    await expect(controller.createGroup(group(["w1", "w2", "w3"]), "alice")).rejects.toThrow(/exceeds/);
  });

  it("accepts a group exactly at the cap", async () => {
    const { controller } = build({ maxGroupSize: 2 });
    await expect(controller.createGroup(group(["w1", "w2"]), "alice")).resolves.toBe("g1");
  });

  it("refuses an error pattern that could backtrack catastrophically", async () => {
    // The pattern is matched against every output delta, so a pathological
    // one is a denial of service against the whole fan-out. It is validated
    // at creation rather than on the hot path.
    const { controller } = build();
    await expect(controller.createGroup(group(["w1"], { errorPattern: "(a+)+" }), "alice")).rejects.toThrow(
      PromptRegexError,
    );
  });

  it("refuses an over-long error pattern", async () => {
    const { controller } = build();
    await expect(controller.createGroup(group(["w1"], { errorPattern: "a".repeat(201) }), "alice")).rejects.toThrow(
      PromptRegexError,
    );
  });

  it("accepts a sane error pattern", async () => {
    const { controller } = build();
    await expect(controller.createGroup(group(["w1"], { errorPattern: "ERROR" }), "alice")).resolves.toBe("g1");
  });

  it("shows a group to its creator and its grantees", async () => {
    const { controller } = build();
    await controller.createGroup(group(["w1"], { grants: ["bob"] }), "alice");
    expect((await controller.getGroup("g1", "alice"))?.groupId).toBe("g1");
    expect((await controller.getGroup("g1", "bob"))?.groupId).toBe("g1");
    expect(await controller.getGroup("g1", "eve")).toBeUndefined();
    expect(await controller.getGroup("nope", "alice")).toBeUndefined();
  });

  it("lists only what a principal may see", async () => {
    const { controller } = build();
    await controller.createGroup(group(["w1"]), "alice");
    expect((await controller.listGroups("alice")).map((entry) => entry.groupId)).toStrictEqual(["g1"]);
    expect(await controller.listGroups("eve")).toStrictEqual([]);
  });

  it("lets a grantee delete nothing they do not own", async () => {
    // Reading a group is not permission to destroy it, so delete checks the
    // same authorization and then simply does nothing for a stranger.
    const { controller, store } = build();
    await controller.createGroup(group(["w1"], { grants: ["bob"] }), "alice");
    await controller.deleteGroup("g1", "eve");
    expect(await store.get("g1")).toBeDefined();
    await controller.deleteGroup("g1", "alice");
    expect(await store.get("g1")).toBeUndefined();
  });

  it("only lets the creator grant access", async () => {
    // A grantee sharing the group onwards would let access spread without
    // the owner ever seeing it.
    const { controller, store } = build();
    await controller.createGroup(group(["w1"], { grants: ["bob"] }), "alice");
    await controller.grantAccess("g1", "eve", "bob");
    expect((await store.get("g1"))?.grants).toStrictEqual(["bob"]);
    await controller.grantAccess("g1", "carol", "alice");
    expect((await store.get("g1"))?.grants).toStrictEqual(["bob", "carol"]);
  });

  it("does not duplicate an existing grant", async () => {
    const { controller, store } = build();
    await controller.createGroup(group(["w1"], { grants: ["bob"] }), "alice");
    await controller.grantAccess("g1", "bob", "alice");
    expect((await store.get("g1"))?.grants).toStrictEqual(["bob"]);
  });

  it("ignores a grant on a group that does not exist", async () => {
    const { controller } = build();
    await expect(controller.grantAccess("nope", "bob", "alice")).resolves.toBeUndefined();
  });
});

describe("FanOutController send authorization", () => {
  it("returns an empty result rather than leaking whether a group exists", async () => {
    // A stranger asking about someone else's group learns nothing from the
    // shape of the answer.
    const { controller, hub } = build();
    await controller.createGroup(group(["w1"]), "alice");
    const result = await controller.send("g1", "uptime", actor("eve", ["viewer"]));
    expect(result.results).toStrictEqual([]);
    expect(result.failedSessions).toStrictEqual([]);
    expect(hub.sent).toStrictEqual([]);
  });

  it("returns an empty result for a group that does not exist", async () => {
    const { controller } = build();
    const result = await controller.send("nope", "uptime", actor("alice"));
    expect(result.groupId).toBe("nope");
    expect(result.results).toStrictEqual([]);
  });

  it("refuses a global admin who guesses a group they were not granted", async () => {
    const { controller, hub } = build();
    await controller.createGroup(group(["w1"]), "alice");

    const result = await controller.send("g1", "uptime", actor("mallory"));

    expect(result.error).toBe("fan-out group not found");
    expect(hub.sent).toStrictEqual([]);
    expect(hub.broadcasts).toStrictEqual([]);
  });
});

describe("FanOutController policy", () => {
  it("uses the caller's actual strongest normalized role", async () => {
    let policyRole: unknown;
    const { controller } = build({
      isGlobalAdmin: async () => true,
      policyGate: {
        interceptFanout: async (_command, context) => {
          policyRole = context.role;
          return { action: "deny" };
        },
      },
    });
    await controller.createGroup(group(["w1"]), "operator");

    await controller.send("g1", "id", actor("operator", ["operator", "viewer"]));

    expect(policyRole).toBe("operator");
  });

  it("blocks a denied command without sending anything", async () => {
    const { controller, hub } = build({
      policyGate: { interceptFanout: async () => ({ action: "deny", reason: "no rm -rf" }) },
    });
    await controller.createGroup(group(["w1"]), "alice");
    const result = await controller.send("g1", "rm -rf /", actor("alice"));
    expect(result.error).toBe("no rm -rf");
    expect(hub.sent).toStrictEqual([]);
  });

  it("supplies a reason when the gate gives none", async () => {
    const { controller } = build({ policyGate: { interceptFanout: async () => ({ action: "deny" }) } });
    await controller.createGroup(group(["w1"]), "alice");
    expect((await controller.send("g1", "x", actor("alice"))).error).toBe("Command blocked by fan-out policy");
  });

  it("holds a command for approval instead of running it", async () => {
    const { controller, hub } = build({ policyGate: { interceptFanout: async () => ({ action: "hold" }) } });
    await controller.createGroup(group(["w1"]), "alice");
    const result = await controller.send("g1", "reboot", actor("alice"));
    expect(result.approvalRequired).toBe(true);
    expect(result.approvalId).toBe(result.sendId);
    expect(hub.sent).toStrictEqual([]);
    expect(hub.approvals).toHaveLength(1);
  });

  it("audits the hold", async () => {
    // A held command is a security event; it has to be in the log whether or
    // not anyone ever approves it.
    const { controller, hub } = build({ policyGate: { interceptFanout: async () => ({ action: "hold" }) } });
    await controller.createGroup(group(["w1"]), "alice");
    await controller.send("g1", "reboot", actor("alice"));
    expect(hub.events).toHaveLength(1);
    expect(hub.events[0]?.eventType).toBe("terminal.fanout.hold");
    expect(hub.events[0]?.workerId).toBe("group:g1");
  });

  it("truncates a long command in the audit record", async () => {
    // The audit log is not a transcript store; an unbounded command would
    // let a caller write as much as they like into it.
    const { controller, hub } = build({ policyGate: { interceptFanout: async () => ({ action: "hold" }) } });
    await controller.createGroup(group(["w1"]), "alice");
    await controller.send("g1", "x".repeat(900), actor("alice"));
    expect(String(hub.events[0]?.data?.command).length).toBe(500);
  });

  it("leaves no releasable approval when hold auditing fails, including on retry", async () => {
    const { controller, hub } = build({
      newId: () => "audit-failed",
      policyGate: { interceptFanout: async () => ({ action: "hold" }) },
    });
    await controller.createGroup(group(["w1"]), "alice");
    hub.appendEvent = async () => {
      throw new Error("audit unavailable");
    };

    await expect(controller.send("g1", "reboot", actor("alice"))).rejects.toThrow("audit unavailable");
    await expect(controller.send("g1", "reboot", actor("alice"))).rejects.toThrow("audit unavailable");

    expect(hub.approvals).toStrictEqual([]);
    expect(await controller.releaseApprovedCommand("audit-failed")).toBeUndefined();
    expect(hub.sent).toStrictEqual([]);
  });

  it("leaves no releasable approval when registration fails, including on retry", async () => {
    const { controller, hub } = build({
      newId: () => "approval-failed",
      policyGate: { interceptFanout: async () => ({ action: "hold" }) },
    });
    await controller.createGroup(group(["w1"]), "alice");
    hub.addApproval = () => {
      throw new Error("approval unavailable");
    };

    await expect(controller.send("g1", "reboot", actor("alice"))).rejects.toThrow("approval unavailable");
    await expect(controller.send("g1", "reboot", actor("alice"))).rejects.toThrow("approval unavailable");

    expect(hub.events).toHaveLength(2);
    expect(hub.approvals).toStrictEqual([]);
    expect(await controller.releaseApprovedCommand("approval-failed")).toBeUndefined();
    expect(hub.sent).toStrictEqual([]);
  });

  it("runs a held command once it is approved", async () => {
    const { controller, hub } = build({ policyGate: { interceptFanout: async () => ({ action: "hold" }) } });
    await controller.createGroup(group(["w1"]), "alice");
    hub.outputs.set("w1", "ok");
    const held = await controller.send("g1", "reboot", actor("alice"));
    const released = await controller.releaseApprovedCommand(held.approvalId ?? "");
    expect(released?.results.map((entry) => entry.workerId)).toStrictEqual(["w1"]);
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1"]);
  });

  it("refuses to run the same approval twice", async () => {
    // The pending record is consumed on release, so a replayed approval id
    // cannot run the command again.
    const { controller } = build({ policyGate: { interceptFanout: async () => ({ action: "hold" }) } });
    await controller.createGroup(group(["w1"]), "alice");
    const held = await controller.send("g1", "reboot", actor("alice"));
    await controller.releaseApprovedCommand(held.approvalId ?? "");
    expect(await controller.releaseApprovedCommand(held.approvalId ?? "")).toBeUndefined();
  });

  it("refuses an approval it never issued", async () => {
    const { controller } = build();
    expect(await controller.releaseApprovedCommand("never-seen")).toBeUndefined();
  });

  it("remembers the timings the held command was sent with", async () => {
    // The overrides belong to the original request; losing them on release
    // would silently run the approved command with the group's defaults.
    const hub = new FakeHub();
    let seen: { quiesceMs: number; maxMs: number } | undefined;
    hub.collectOutput = async (_workerId: string, options: { quiesceMs: number; maxMs: number }) => {
      seen = options;
      return { output: "", elapsedMs: 0 };
    };
    const controller = new FanOutController({
      hub,
      now: () => NOW,
      newId: () => "held",
      ...security(),
      policyGate: { interceptFanout: async () => ({ action: "hold" }) },
    });
    await controller.createGroup(group(["w1"], { quiesceMs: 1, maxResponseMs: 2 }), "alice");
    const held = await controller.send("g1", "reboot", actor("alice"), { quiesceMs: 111, maxResponseMs: 222 });
    await controller.releaseApprovedCommand(held.approvalId ?? "");
    expect(seen).toMatchObject({ quiesceMs: 111, maxMs: 222 });
  });

  it("re-checks authorization when the approval is released", async () => {
    let isAdmin = true;
    const { controller, hub } = build({
      isGlobalAdmin: async () => isAdmin,
      policyGate: { interceptFanout: async () => ({ action: "hold" }) },
    });
    const principal = actor("alice");
    await controller.createGroup(group(["w1"]), "alice");
    const held = await controller.send("g1", "reboot", principal);

    isAdmin = false;
    const released = await controller.releaseApprovedCommand(held.approvalId ?? "");

    expect(released?.error).toBe("global admin role required");
    expect(hub.sent).toStrictEqual([]);
  });

  it("re-checks every member's session authorization when an approval is released", async () => {
    let readable = true;
    const { controller, hub } = build({
      policyGate: { interceptFanout: async () => ({ action: "hold" }) },
      resolveSession: async (workerId) => ({ workerId }),
      canReadSession: async () => readable,
    });
    await controller.createGroup(group(["w1"]), "alice");
    const held = await controller.send("g1", "reboot", actor("alice"));

    readable = false;
    const released = await controller.releaseApprovedCommand(held.approvalId ?? "");

    expect(hub.sent).toStrictEqual([]);
    expect(released?.failedSessions).toStrictEqual(["w1"]);
  });

  it("re-checks the originating principal's current group grant when an approval is released", async () => {
    const { controller, hub, store } = build({
      policyGate: { interceptFanout: async () => ({ action: "hold" }) },
    });
    await controller.createGroup(group(["w1"], { grants: ["grantee"] }), "alice");
    const held = await controller.send("g1", "reboot", actor("grantee"));
    const stored = await store.get("g1");
    expect(stored).toBeDefined();
    if (stored === undefined) return;
    stored.grants = [];
    await store.save(stored);

    const released = await controller.releaseApprovedCommand(held.approvalId ?? "");

    expect(released?.error).toBe("fan-out group not found");
    expect(hub.sent).toStrictEqual([]);
    expect(hub.broadcasts).toStrictEqual([]);
  });

  it("forgets a pending command when its approval expires", async () => {
    // Otherwise a held command lingers in memory for the life of the process
    // and could still be released long after the window closed.
    const { controller, hub } = build({ policyGate: { interceptFanout: async () => ({ action: "hold" }) } });
    await controller.createGroup(group(["w1"]), "alice");
    const held = await controller.send("g1", "reboot", actor("alice"));
    hub.onApprovalExpired?.(held.approvalId ?? "");
    expect(await controller.releaseApprovedCommand(held.approvalId ?? "")).toBeUndefined();
  });

  it("runs on real defaults when given none", async () => {
    // Every other test pins the clock, the id source and the store; this one
    // checks the production defaults are wired and produce usable values.
    const hub = new FakeHub();
    const controller = new FanOutController({ hub, ...security() });
    await controller.createGroup(
      fanOutGroup({ groupId: "g1", name: "f", workerIds: ["w1"], createdBy: "alice", createdAt: NOW }),
      "alice",
    );
    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(result.sendId).toMatch(/^[0-9a-f]{32}$/);
    expect(Math.abs(result.sentAt - Date.now() / 1000)).toBeLessThan(5);
  });

  it("allows by default when no gate is configured", async () => {
    const { controller, hub } = build();
    await controller.createGroup(group(["w1"]), "alice");
    await controller.send("g1", "uptime", actor("alice"));
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1"]);
  });
});
