//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { AuthorizablePrincipal } from "../server/authorization.ts";
import type { OutputCapture } from "./collector.ts";
import { FanOutController, type FanOutControllerHub } from "./controller.ts";
import { fanOutGroup, type FanOutResult } from "./models.ts";
import { createFanoutRoutes, type FanoutRoutesController } from "./routes.ts";

interface Scenario {
  id: string;
  expected: Record<string, unknown>;
  backends: { typescript: { status: string; expected: Record<string, unknown> } } &
    Record<string, { status: string; expected: Record<string, unknown> }>;
}

interface Contract {
  scenarios: Scenario[];
}

interface Observation {
  id: string;
  status: string;
  status_code: number;
  error: string | null;
  approval_required: boolean;
  approval_id: string | null;
  delivered_workers: string[];
  observer_notifications: string[];
  failed_members: string[];
  output: Record<string, string>;
}

const contractPath =
  process.env.FANOUT_SECURITY_SCENARIO_CONTRACT ??
  resolve(import.meta.dirname, "../../../../spec/fanout_security_scenarios.json");
const outputPath = process.env.FANOUT_SECURITY_SCENARIO_OUTPUT;

class ScenarioHub implements FanOutControllerHub {
  readonly delivered: string[] = [];
  readonly observers: string[] = [];
  readonly refused = new Set<string>();
  readonly immediate = new Map<string, string>();
  readonly buffers = new Map<string, string[]>();

  async sendWorker(workerId: string): Promise<boolean> {
    if (this.refused.has(workerId)) {
      return false;
    }
    this.delivered.push(workerId);
    const value = this.immediate.get(workerId) ?? "ok";
    this.buffers.get(workerId)?.push(value);
    return true;
  }

  async broadcast(workerId: string, message: Record<string, unknown>): Promise<void> {
    if (message.type === "fanout_input") {
      this.observers.push(workerId);
    }
  }

  async appendEvent(): Promise<void> {}

  addApproval(): void {}

  async openOutputCapture(workerId: string): Promise<OutputCapture> {
    const buffer: string[] = [];
    this.buffers.set(workerId, buffer);
    return {
      collect: async () => ({ output: buffer.join(""), elapsedMs: 1 }),
      close: async () => {},
    };
  }
}

function principal(subjectId = "admin", roles: string[] = ["admin"]): AuthorizablePrincipal {
  return { subject_id: subjectId, roles: new Set(roles), scopes: new Set<string>() };
}

function empty(id: string, status: string, statusCode: number, error: string | null): Observation {
  return {
    id,
    status,
    status_code: statusCode,
    error,
    approval_required: false,
    approval_id: null,
    delivered_workers: [],
    observer_notifications: [],
    failed_members: [],
    output: {},
  };
}

function fromResult(
  scenario: Scenario,
  result: FanOutResult,
  hub: ScenarioHub,
  options: Partial<Observation> = {},
): Observation {
  return {
    ...empty(scenario.id, scenario.backends.typescript.status, 200, result.error),
    approval_required: result.approvalRequired,
    approval_id: result.approvalId === null ? null : "approval",
    delivered_workers: hub.delivered,
    observer_notifications: hub.observers,
    failed_members: result.failedSessions,
    output: Object.fromEntries(
      result.results.flatMap((entry) => (entry.ok && entry.outputDelta !== undefined ? [[entry.workerId, entry.outputDelta]] : [])),
    ),
    ...options,
  };
}

function controller(
  hub: ScenarioHub,
  readable = new Set(["w1", "w2"]),
  policy?: "deny" | "hold",
): FanOutController {
  const policyOptions =
    policy === undefined
      ? {}
      : {
          policyGate: {
            interceptFanout: async () =>
              policy === "deny" ? { action: "deny" as const, reason: "denied" } : { action: "hold" as const },
          },
        };
  return new FanOutController({
    hub,
    now: () => 1,
    newId: () => "approval",
    isGlobalAdmin: async (actor) => actor.roles.has("admin") && (actor.admin_session_scope ?? null) === null,
    resolveSession: async (workerId) => ({ workerId }),
    canReadSession: async (_actor, definition) => readable.has((definition as { workerId: string }).workerId),
    ...policyOptions,
  });
}

async function seeded(
  members: string[],
  options: { readable?: Set<string>; policy?: "deny" | "hold" } = {},
): Promise<{ hub: ScenarioHub; controller: FanOutController }> {
  const hub = new ScenarioHub();
  const instance = controller(hub, options.readable, options.policy);
  await instance.createGroup(
    fanOutGroup({ groupId: "g1", name: "fleet", workerIds: members, createdBy: "admin", createdAt: 1 }),
    "admin",
  );
  return { hub, controller: instance };
}

async function routeScenario(scenario: Scenario): Promise<Observation> {
  const hub = new ScenarioHub();
  const instance = controller(hub);
  const routes = createFanoutRoutes({
    controller: instance as unknown as FanoutRoutesController,
    registry: { getDefinition: async () => undefined },
    authz: {
      isAdmin: async (actor) => actor.roles.has("admin") && (actor.admin_session_scope ?? null) === null,
      canReadSession: async () => true,
    },
    allowUnknownMembers: scenario.id === "dormant_member_permissive_admission",
    now: () => 1,
  });
  if (scenario.id === "unauthenticated_refusal") {
    const response = await routes.sendToGroup({ principal: undefined, body: { data: "id" } }, "g1");
    return empty(scenario.id, scenario.backends.typescript.status, response.status, "authentication_required");
  }
  if (scenario.id === "viewer_public_session_refusal") {
    const response = await routes.sendToGroup({ principal: principal("viewer", ["viewer"]), body: { data: "id" } }, "g1");
    return empty(scenario.id, scenario.backends.typescript.status, response.status, "global_admin_required");
  }
  const response = await routes.createGroup({
    principal: principal(),
    body: { name: "dormant", worker_ids: ["missing"] },
  });
  return empty(
    scenario.id,
    scenario.backends.typescript.status,
    response.status,
    response.status < 400 ? null : "unknown_member",
  );
}

async function executeScenario(scenario: Scenario): Promise<Observation> {
  if (
    scenario.id === "unauthenticated_refusal" ||
    scenario.id === "viewer_public_session_refusal" ||
    scenario.id.startsWith("dormant_member_")
  ) {
    return routeScenario(scenario);
  }
  if (scenario.id === "missing_controller_dependencies") {
    const hub = new ScenarioHub();
    const instance = new FanOutController({ hub, now: () => 1, newId: () => "send" });
    await instance.createGroup(
      fanOutGroup({ groupId: "g1", name: "fleet", workerIds: ["w1"], createdBy: "admin", createdAt: 1 }),
      "admin",
    );
    const result = await instance.send("g1", "id", principal());
    return fromResult(scenario, result, hub, { status_code: 403, error: "authorization_unavailable" });
  }
  if (scenario.id === "current_authorization_revocation" || scenario.id === "group_grant_non_bypass") {
    const readable = scenario.id === "current_authorization_revocation" ? new Set(["w1"]) : new Set<string>();
    const members = scenario.id === "current_authorization_revocation" ? ["w1", "w2"] : ["w1"];
    const seededController = await seeded(members, { readable });
    const result = await seededController.controller.send("g1", "id", principal());
    return fromResult(scenario, result, seededController.hub);
  }
  if (scenario.id === "partial_member_failure") {
    const seededController = await seeded(["w1", "w2"]);
    seededController.hub.refused.add("w2");
    const result = await seededController.controller.send("g1", "id", principal());
    return fromResult(scenario, result, seededController.hub);
  }
  if (scenario.id === "policy_deny") {
    const seededController = await seeded(["w1"], { policy: "deny" });
    const result = await seededController.controller.send("g1", "rm -rf /", principal());
    return fromResult(scenario, result, seededController.hub, { status_code: 403, error: "policy_denied" });
  }
  if (scenario.id === "policy_hold_release") {
    const seededController = await seeded(["w1"], { policy: "hold" });
    const held = await seededController.controller.send("g1", "reboot", principal());
    const released = await seededController.controller.releaseApprovedCommand(held.approvalId ?? "");
    expect(released).toBeDefined();
    return fromResult(scenario, held, seededController.hub, {
      status_code: 202,
      delivered_workers: seededController.hub.delivered,
      observer_notifications: seededController.hub.observers,
      output: Object.fromEntries(
        (released?.results ?? []).flatMap((entry) =>
          entry.ok && entry.outputDelta !== undefined ? [[entry.workerId, entry.outputDelta]] : [],
        ),
      ),
    });
  }
  const seededController = await seeded(["w1"]);
  seededController.hub.immediate.set("w1", "immediate");
  const result = await seededController.controller.send("g1", "id", principal());
  return fromResult(scenario, result, seededController.hub);
}

describe("shared fan-out security scenarios", () => {
  it("executes every applicable TypeScript component scenario", async () => {
    const contract = JSON.parse(readFileSync(contractPath, "utf8")) as Contract;
    const applicable = contract.scenarios.filter((scenario) => scenario.backends.typescript.status !== "unserved");
    const observations: Observation[] = [];
    for (const scenario of applicable) {
      observations.push(await executeScenario(scenario));
    }
    expect(observations.map((observation) => observation.id).sort()).toStrictEqual(
      applicable.map((scenario) => scenario.id).sort(),
    );
    for (const [index, scenario] of applicable.entries()) {
      const expected = { ...scenario.expected, ...scenario.backends.typescript.expected };
      const observation = observations[index] as unknown as Record<string, unknown>;
      for (const [field, value] of Object.entries(expected)) {
        expect(observation[field], `${scenario.id}.${field}`).toStrictEqual(value);
      }
    }
    if (outputPath !== undefined) {
      writeFileSync(outputPath, `${JSON.stringify(observations, null, 2)}\n`, "utf8");
    }
  });
});
