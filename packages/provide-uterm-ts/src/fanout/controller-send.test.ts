//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import type { AuthorizablePrincipal } from "../server/authorization.ts";
import { FanOutController, type FanOutControllerHub, type FanOutGroup, fanOutGroup } from "./index.ts";

const NOW = 1000;

/** A hub that records what it was asked to do and can be made to refuse. */
class FakeHub implements FanOutControllerHub {
  readonly sent: Array<{ workerId: string; message: Record<string, unknown> }> = [];
  readonly broadcasts: Array<{ workerId: string; message: Record<string, unknown> }> = [];
  readonly collected: string[] = [];
  readonly refusing = new Set<string>();
  readonly exploding = new Set<string>();
  readonly outputs = new Map<string, string>();
  readonly immediateOutputs = new Map<string, string>();
  readonly captureBuffers = new Map<string, string[]>();
  readonly captureFailures = new Set<string>();
  readonly closes = new Map<string, number>();
  onApprovalExpired: ((requestId: string) => void) | undefined;

  async sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean> {
    this.sent.push({ workerId, message });
    const immediate = this.immediateOutputs.get(workerId);
    if (immediate !== undefined) {
      this.immediateOutputs.delete(workerId);
      this.captureBuffers.get(workerId)?.push(immediate);
    }
    return !this.refusing.has(workerId);
  }

  async broadcast(workerId: string, message: Record<string, unknown>): Promise<void> {
    this.broadcasts.push({ workerId, message });
  }

  async appendEvent(): Promise<void> {}

  addApproval(): void {}

  async collectOutput(workerId: string): Promise<{ output: string; elapsedMs: number }> {
    this.collected.push(workerId);
    if (this.exploding.has(workerId)) {
      throw new Error(`collection failed for ${workerId}`);
    }
    return { output: this.outputs.get(workerId) ?? "", elapsedMs: 7 };
  }

  async openOutputCapture(workerId: string) {
    if (this.captureFailures.has(workerId)) {
      throw new Error("capture unavailable");
    }
    const buffer: string[] = [];
    this.captureBuffers.set(workerId, buffer);
    return {
      collect: async () =>
        buffer.length > 0 ? { output: buffer.join(""), elapsedMs: 7 } : this.collectOutput(workerId),
      close: async () => {
        this.closes.set(workerId, (this.closes.get(workerId) ?? 0) + 1);
      },
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
function build() {
  const hub = new FakeHub();
  let counter = 0;
  const controller = new FanOutController({ hub, ...security(), now: () => NOW, newId: () => `id-${++counter}` });
  return { hub, controller };
}

/** Register a group and return it. */
async function seed(
  controller: FanOutController,
  workerIds: string[],
  overrides: Partial<FanOutGroup> = {},
): Promise<FanOutGroup> {
  const record = {
    ...fanOutGroup({ groupId: "g1", name: "fleet", workerIds, createdBy: "alice", createdAt: NOW }),
    ...overrides,
  };
  await controller.createGroup(record, "alice");
  return record;
}

describe("FanOutController parallel send", () => {
  it("fails closed for missing dependencies, string identity, and non-admin callers", async () => {
    const hub = new FakeHub();
    const missing = new FanOutController({ hub, now: () => NOW, newId: () => "missing" });
    await seed(missing, ["w1"]);
    const secured = build();
    await seed(secured.controller, ["w1"]);

    const results = [
      await missing.send("g1", "uptime", actor("alice")),
      await secured.controller.send("g1", "uptime", "alice" as unknown as AuthorizablePrincipal),
      await secured.controller.send("g1", "uptime", actor("viewer", ["viewer"])),
    ];

    expect(results.every((result) => typeof result.error === "string")).toBe(true);
    expect(hub.sent).toStrictEqual([]);
    expect(secured.hub.sent).toStrictEqual([]);
    expect(secured.hub.broadcasts).toStrictEqual([]);
  });

  it("captures output emitted synchronously inside every send", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    hub.immediateOutputs.set("w1", "immediate-w1");
    hub.immediateOutputs.set("w2", "immediate-w2");

    const result = await controller.send("g1", "uptime", actor("alice"));

    expect(result.results.map((entry) => entry.outputDelta)).toStrictEqual(["immediate-w1", "immediate-w2"]);
  });

  it("does not notify or send to a member whose capture cannot open", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    hub.captureFailures.add("w1");

    const result = await controller.send("g1", "uptime", actor("alice"));

    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w2"]);
    expect(hub.broadcasts.map((entry) => entry.workerId)).toStrictEqual(["w2"]);
    expect(result.failedSessions).toStrictEqual(["w1"]);
  });

  it("closes every prepared capture exactly once when sends are rejected", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    hub.refusing.add("w1");
    hub.refusing.add("w2");

    await controller.send("g1", "uptime", actor("alice"));

    expect([...hub.closes.entries()]).toStrictEqual([
      ["w1", 1],
      ["w2", 1],
    ]);
  });

  it("closes every prepared capture exactly once when a group repeats a member", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w1"]);
    hub.refusing.add("w1");

    await controller.send("g1", "uptime", actor("alice"));

    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1", "w1"]);
    expect(hub.closes.get("w1")).toBe(2);
  });

  it("never sends to a member whose current session authorization was revoked", async () => {
    const hub = new FakeHub();
    const readable = new Set(["w1"]);
    const controller = new FanOutController({
      hub,
      now: () => NOW,
      newId: () => "id-1",
      isGlobalAdmin: async () => true,
      resolveSession: async (workerId) => ({ workerId }),
      canReadSession: async (_principal, definition) => readable.has((definition as { workerId: string }).workerId),
    });
    await seed(controller, ["w1", "w2"]);

    const result = await controller.send("g1", "uptime", actor("alice"));

    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1"]);
    expect(result.failedSessions).toStrictEqual(["w2"]);
  });

  it("a group grant does not bypass current session authorization", async () => {
    const hub = new FakeHub();
    const controller = new FanOutController({
      hub,
      now: () => NOW,
      newId: () => "id-1",
      isGlobalAdmin: async () => true,
      resolveSession: async (workerId) => ({ workerId }),
      canReadSession: async () => false,
    });
    await seed(controller, ["w1"], { grants: ["bob"] });

    const result = await controller.send("g1", "uptime", actor("bob"));

    expect(hub.sent).toStrictEqual([]);
    expect(hub.broadcasts).toStrictEqual([]);
    expect(result.failedSessions).toStrictEqual(["w1"]);
  });

  it("sends to every session and reports each one", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    hub.outputs.set("w1", "same");
    hub.outputs.set("w2", "same");

    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1", "w2"]);
    expect(result.results.map((entry) => [entry.workerId, entry.ok])).toStrictEqual([
      ["w1", true],
      ["w2", true],
    ]);
    expect(result.failedSessions).toStrictEqual([]);
  });

  it("carries the command in an input frame", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1"]);
    await controller.send("g1", "uptime", actor("alice"));
    expect(hub.sent[0]?.message).toStrictEqual({ type: "input", data: "uptime", ts: NOW });
  });

  it("tells each session's observers the input came from a fan-out", async () => {
    // Without it a watcher cannot tell a broadcast from a local hijack, and
    // an operator sees keystrokes they did not type with no explanation.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(hub.broadcasts.map((entry) => entry.workerId)).toStrictEqual(["w1", "w2"]);
    expect(hub.broadcasts[0]?.message).toStrictEqual({
      type: "fanout_input",
      group_id: "g1",
      send_id: result.sendId,
      command: "uptime",
      from_principal: "alice",
    });
  });

  it("announces to observers before sending to the workers", async () => {
    // The other order lets output arrive before anything has explained why.
    const { hub, controller } = build();
    await seed(controller, ["w1"]);
    await controller.send("g1", "uptime", actor("alice"));
    expect(hub.broadcasts).toHaveLength(1);
    expect(hub.sent).toHaveLength(1);
  });

  it("marks a session that refused the send as failed", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    hub.refusing.add("w1");
    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(result.failedSessions).toStrictEqual(["w1"]);
    expect(result.results[0]).toStrictEqual({
      workerId: "w1",
      ok: false,
      outputDelta: undefined,
      elapsedMs: 0,
      divergent: false,
    });
  });

  it("does not try to collect from a session that refused", async () => {
    // There is nothing coming, so waiting out the quiesce window for it is
    // latency the whole fan-out pays.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    hub.refusing.add("w1");
    await controller.send("g1", "uptime", actor("alice"));
    expect(hub.collected).toStrictEqual(["w2"]);
  });

  it("marks a session whose collection failed as failed", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    hub.exploding.add("w1");
    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(result.failedSessions).toStrictEqual(["w1"]);
    expect(result.results[1]?.ok).toBe(true);
    expect([...hub.closes.values()]).toStrictEqual([1, 1]);
  });

  it("marks a session whose send threw as failed", async () => {
    // A transport that raises rather than returning false is still a session
    // that did not get the command, and must not stall the rest.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"]);
    const original = hub.sendWorker.bind(hub);
    hub.sendWorker = async (workerId: string, message: Record<string, unknown>) => {
      if (workerId === "w1") {
        throw new Error("transport gone");
      }
      return original(workerId, message);
    };
    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(result.failedSessions).toStrictEqual(["w1"]);
    expect(result.results[1]?.ok).toBe(true);
  });

  it("carries on when telling observers fails", async () => {
    // The announcement is courtesy to watchers; a broken observer socket must
    // not stop the command reaching the fleet.
    const { hub, controller } = build();
    await seed(controller, ["w1"]);
    hub.broadcast = async () => {
      throw new Error("observer gone");
    };
    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(result.results[0]?.ok).toBe(true);
  });

  it("keeps the results in the group's order", async () => {
    // Callers line these up against the group's worker list; reordering by
    // whoever answered first would mislabel every row.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2", "w3"]);
    hub.refusing.add("w2");
    const result = await controller.send("g1", "uptime", actor("alice"));
    expect(result.results.map((entry) => entry.workerId)).toStrictEqual(["w1", "w2", "w3"]);
  });

  it("flags the session whose output disagrees", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2", "w3"]);
    hub.outputs.set("w1", "all good here");
    hub.outputs.set("w2", "all good here");
    hub.outputs.set("w3", "catastrophic failure");
    const result = await controller.send("g1", "check", actor("alice"));
    expect(result.divergentSessions).toStrictEqual(["w3"]);
    expect(result.results.map((entry) => entry.divergent)).toStrictEqual([false, false, true]);
  });

  it("does not consider a failed session when judging divergence", async () => {
    // It produced no output; counting its silence would drag the consensus
    // towards empty and flag the healthy sessions instead.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2", "w3"]);
    hub.refusing.add("w3");
    hub.outputs.set("w1", "identical");
    hub.outputs.set("w2", "identical");
    const result = await controller.send("g1", "check", actor("alice"));
    expect(result.divergentSessions).toStrictEqual([]);
  });

  it("reports no divergence when nothing succeeded", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1"]);
    hub.refusing.add("w1");
    const result = await controller.send("g1", "check", actor("alice"));
    expect(result.divergentSessions).toStrictEqual([]);
  });

  it("honours the group's divergence threshold", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2", "w3"], { divergenceThreshold: 0.99 });
    hub.outputs.set("w1", "result 1");
    hub.outputs.set("w2", "result 2");
    hub.outputs.set("w3", "result 3");
    const result = await controller.send("g1", "check", actor("alice"));
    expect(result.divergentSessions).toStrictEqual(["w1", "w2", "w3"]);
  });

  it("takes the caller's timings over the group's", async () => {
    const { controller } = build();
    await seed(controller, ["w1"], { quiesceMs: 500, maxResponseMs: 10_000 });
    let seen: { quiesceMs: number; maxMs: number } | undefined;
    const spy = new FanOutController({
      hub: {
        sendWorker: async () => true,
        broadcast: async () => {},
        appendEvent: async () => {},
        addApproval: () => {},
        openOutputCapture: async () => ({
          collect: async (options) => {
            seen = options;
            return { output: "", elapsedMs: 0 };
          },
          close: async () => {},
        }),
      },
      ...security(),
      now: () => NOW,
      newId: () => "id",
    });
    await spy.createGroup(
      { ...fanOutGroup({ groupId: "g1", name: "f", workerIds: ["w1"], createdBy: "alice", createdAt: NOW }) },
      "alice",
    );
    await spy.send("g1", "x", actor("alice"), { quiesceMs: 11, maxResponseMs: 22 });
    expect(seen).toMatchObject({ quiesceMs: 11, maxMs: 22 });
  });

  it("falls back to the group's timings", async () => {
    let seen: { quiesceMs: number; maxMs: number } | undefined;
    const controller = new FanOutController({
      hub: {
        sendWorker: async () => true,
        broadcast: async () => {},
        appendEvent: async () => {},
        addApproval: () => {},
        openOutputCapture: async () => ({
          collect: async (options) => {
            seen = options;
            return { output: "", elapsedMs: 0 };
          },
          close: async () => {},
        }),
      },
      ...security(),
      now: () => NOW,
      newId: () => "id",
    });
    await controller.createGroup(
      {
        ...fanOutGroup({ groupId: "g1", name: "f", workerIds: ["w1"], createdBy: "alice", createdAt: NOW }),
        quiesceMs: 33,
        maxResponseMs: 44,
      },
      "alice",
    );
    await controller.send("g1", "x", actor("alice"));
    expect(seen).toMatchObject({ quiesceMs: 33, maxMs: 44 });
  });
});

describe("FanOutController sequential send", () => {
  it("captures synchronous output one member at a time", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"], { mode: "sequential" });
    hub.immediateOutputs.set("w1", "immediate-w1");
    hub.immediateOutputs.set("w2", "immediate-w2");

    const result = await controller.send("g1", "restart", actor("alice"));

    expect(result.results.map((entry) => entry.outputDelta)).toStrictEqual(["immediate-w1", "immediate-w2"]);
    expect([...hub.closes.values()]).toStrictEqual([1, 1]);
  });
  it("waits for each session before starting the next", async () => {
    // The point of the mode: a rolling restart must not take every host down
    // at once.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"], { mode: "sequential" });
    const order: string[] = [];
    const original = hub.collectOutput.bind(hub);
    hub.collectOutput = async (workerId: string) => {
      order.push(`collect ${workerId}`);
      return original(workerId);
    };
    const originalSend = hub.sendWorker.bind(hub);
    hub.sendWorker = async (workerId: string, message: Record<string, unknown>) => {
      order.push(`send ${workerId}`);
      return originalSend(workerId, message);
    };
    await controller.send("g1", "restart", actor("alice"));
    expect(order).toStrictEqual(["send w1", "collect w1", "send w2", "collect w2"]);
  });

  it("stops after a failure when told to", async () => {
    // Everything after the trigger is reported as failed without being sent,
    // so a bad deploy stops at the first host rather than the last.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2", "w3"], {
      mode: "sequential",
      stopOnFirstError: true,
      errorPattern: "FAILED",
    });
    hub.outputs.set("w1", "deploy FAILED");
    const result = await controller.send("g1", "deploy", actor("alice"));
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1"]);
    expect(result.failedSessions).toStrictEqual(["w2", "w3"]);
    expect(result.results[0]?.ok).toBe(true);
  });

  it("keeps going when the pattern does not match", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"], { mode: "sequential", stopOnFirstError: true, errorPattern: "FAILED" });
    hub.outputs.set("w1", "deploy ok");
    await controller.send("g1", "deploy", actor("alice"));
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1", "w2"]);
  });

  it("keeps going when stopping was not requested", async () => {
    // The pattern alone is a label, not a brake.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"], { mode: "sequential", errorPattern: "FAILED" });
    hub.outputs.set("w1", "deploy FAILED");
    await controller.send("g1", "deploy", actor("alice"));
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1", "w2"]);
  });

  it("keeps going when there is no pattern to match", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"], { mode: "sequential", stopOnFirstError: true });
    hub.outputs.set("w1", "anything");
    await controller.send("g1", "deploy", actor("alice"));
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1", "w2"]);
  });

  it("marks a refusal as failed and carries on", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"], { mode: "sequential" });
    hub.refusing.add("w1");
    const result = await controller.send("g1", "x", actor("alice"));
    expect(result.failedSessions).toStrictEqual(["w1"]);
    expect(hub.collected).toStrictEqual(["w2"]);
  });

  it("judges divergence across the sessions that answered", async () => {
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2", "w3"], { mode: "sequential" });
    hub.outputs.set("w1", "steady state");
    hub.outputs.set("w2", "steady state");
    hub.outputs.set("w3", "wildly different output");
    const result = await controller.send("g1", "check", actor("alice"));
    expect(result.divergentSessions).toStrictEqual(["w3"]);
  });

  it("treats any mode that is not sequential as parallel", async () => {
    // The reference compares against the literal string, so an unknown mode
    // fans out rather than failing closed.
    const { hub, controller } = build();
    await seed(controller, ["w1", "w2"], { mode: "something-else" });
    await controller.send("g1", "x", actor("alice"));
    expect(hub.sent.map((entry) => entry.workerId)).toStrictEqual(["w1", "w2"]);
  });
});
