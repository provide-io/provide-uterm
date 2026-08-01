//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { AuthorizablePrincipal } from "../server/authorization.ts";
import type { OutputCapture } from "./collector.ts";
import { type ApprovalIdentity, FanOutController, type FanOutControllerHub, type SendOptions } from "./controller.ts";
import { type FanOutResult, fanOutGroup, InMemoryFanOutStore } from "./models.ts";
import { createFanoutRoutes, type FanoutRoutesController } from "./routes.ts";

interface ActorInput {
  subject: string;
  authenticated: boolean;
  roles: string[];
}

interface GroupInput {
  id: string;
  creator: string;
  members: string[];
  grants: string[];
  allow_unknown_members: boolean;
}

interface ScenarioInput {
  surface: "rest" | "rest_release" | "controller" | "store";
  operation: string;
  actor: ActorInput;
  group: GroupInput;
  visibility: { readable_members: string[]; revoke_before_send: string[] };
  policy: { action: "allow" | "deny" | "hold_release" };
  workers: {
    accepted_members: string[];
    immediate_output: Record<string, string>;
    continuous_output?: boolean;
  };
  command: string;
  omit_authorizers?: boolean;
  mutation_member?: string;
  concurrent_grants?: string[];
  max_response_ms?: number;
}

interface Scenario {
  id: string;
  input: ScenarioInput;
  expected: Record<string, unknown>;
  backends: {
    typescript: { status: string; expected: Record<string, unknown> };
  } & Record<string, { status: string; expected: Record<string, unknown> }>;
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
  command: string;
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
  readonly buffers = new Map<string, string[]>();
  readonly approvals: Record<string, unknown>[] = [];
  private readonly accepted: Set<string>;
  private readonly immediate: Record<string, string>;
  #nextApprovalRevision = 0;

  constructor(accepted: Set<string>, immediate: Record<string, string>) {
    this.accepted = accepted;
    this.immediate = immediate;
  }

  async sendWorker(workerId: string): Promise<boolean> {
    if (!this.accepted.has(workerId)) return false;
    this.delivered.push(workerId);
    this.buffers.get(workerId)?.push(this.immediate[workerId] ?? "ok");
    return true;
  }

  async broadcast(workerId: string, message: Record<string, unknown>): Promise<void> {
    if (message.type === "fanout_input") this.observers.push(workerId);
  }

  async appendEvent(): Promise<void> {}

  addApproval(request: Record<string, unknown>): ApprovalIdentity | undefined {
    const id = String(request.id);
    if (this.approvals.some((approval) => approval.id === id)) return undefined;
    const revision = ++this.#nextApprovalRevision;
    this.approvals.push({ ...request, revision });
    return { id, revision };
  }

  async openOutputCapture(workerId: string): Promise<OutputCapture> {
    const buffer: string[] = [];
    this.buffers.set(workerId, buffer);
    return {
      collect: async () => ({ output: buffer.join(""), elapsedMs: 1 }),
      close: async () => {},
    };
  }
}

function actor(input: ActorInput): AuthorizablePrincipal | undefined {
  if (!input.authenticated) return undefined;
  return {
    subject_id: input.subject,
    roles: new Set(input.roles),
    scopes: new Set<string>(),
  };
}

function canonicalRouteError(status: number, body: unknown): string | null {
  if (status < 400) return null;
  const record = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const message = String(record.error ?? record.detail ?? "");
  if (status === 401) return "authentication_required";
  if (message.includes("admin")) return "global_admin_required";
  if (message.includes("unknown fan-out")) return "unknown_member";
  return message || "request_failed";
}

function base(scenario: Scenario, statusCode: number, command: string, error: string | null): Observation {
  return {
    id: scenario.id,
    status: scenario.backends.typescript.status,
    status_code: statusCode,
    error,
    approval_required: false,
    approval_id: null,
    command,
    delivered_workers: [],
    observer_notifications: [],
    failed_members: [],
    output: {},
  };
}

function fromResult(scenario: Scenario, result: FanOutResult, hub: ScenarioHub, statusCode = 200): Observation {
  return {
    ...base(scenario, statusCode, result.command, result.error),
    approval_required: result.approvalRequired,
    approval_id: result.approvalId === null ? null : "approval",
    delivered_workers: [...hub.delivered],
    observer_notifications: [...hub.observers],
    failed_members: [...result.failedSessions],
    output: Object.fromEntries(
      result.results.flatMap((entry) =>
        entry.ok && entry.outputDelta !== undefined ? [[entry.workerId, entry.outputDelta]] : [],
      ),
    ),
  };
}

async function buildController(input: ScenarioInput): Promise<{ controller: FanOutController; hub: ScenarioHub }> {
  const readable = new Set(input.visibility.readable_members);
  for (const revoked of input.visibility.revoke_before_send) readable.delete(revoked);
  const hub = new ScenarioHub(new Set(input.workers.accepted_members), input.workers.immediate_output);
  const policy = input.policy.action;
  const controller = new FanOutController({
    hub,
    now: () => 1,
    newId: () => "approval",
    allowUnknownMembers: input.group.allow_unknown_members,
    ...(input.omit_authorizers === true
      ? {}
      : {
          isGlobalAdmin: async (principal) =>
            principal.roles.has("admin") && (principal.admin_session_scope ?? null) === null,
          resolveSession: async (workerId) => ({ workerId }),
          canReadSession: async (_principal, definition) => readable.has((definition as { workerId: string }).workerId),
        }),
    ...(policy === "allow"
      ? {}
      : {
          policyGate: {
            interceptFanout: async () =>
              policy === "deny" ? { action: "deny" as const, reason: "policy_denied" } : { action: "hold" as const },
          },
        }),
  });
  const group = fanOutGroup({
    groupId: input.group.id,
    name: "fixture-group",
    workerIds: [...input.group.members],
    createdBy: input.group.creator,
    createdAt: 1,
    grants: [...input.group.grants],
    maxResponseMs: input.max_response_ms ?? 100,
    quiesceMs: 1,
  });
  await controller.createGroup(group, input.group.creator);
  return { controller, hub };
}

async function executeRest(scenario: Scenario): Promise<Observation> {
  const input = scenario.input;
  const built = await buildController(input);
  const definitions = new Set(input.visibility.readable_members);
  const routes = createFanoutRoutes({
    controller: routeController(built.controller),
    registry: {
      getDefinition: async (workerId) => (definitions.has(workerId) ? { workerId } : undefined),
    },
    authz: {
      isAdmin: async (principal) => principal.roles.has("admin") && (principal.admin_session_scope ?? null) === null,
    },
    now: () => 1,
    newId: () => input.group.id,
  });
  const requestActor = actor(input.actor);
  if (input.operation === "create") {
    const response = await routes.createGroup({
      principal: requestActor,
      body: { name: "fixture-group", worker_ids: input.group.members },
    });
    return base(scenario, response.status, input.command, canonicalRouteError(response.status, response.body));
  }
  const response = await routes.sendToGroup(
    {
      principal: requestActor,
      body: { data: input.command, max_response_ms: input.max_response_ms },
    },
    input.group.id,
  );
  const body =
    typeof response.body === "object" && response.body !== null ? (response.body as Record<string, unknown>) : {};
  if (response.status !== 200 || body.results === undefined) {
    return base(scenario, response.status, input.command, canonicalRouteError(response.status, response.body));
  }
  const held: FanOutResult = {
    groupId: String(body.group_id),
    sendId: String(body.send_id),
    command: String(body.command),
    sentAt: Number(body.sent_at),
    results: [],
    divergentSessions: [],
    failedSessions: Array.isArray(body.failed_sessions) ? (body.failed_sessions as string[]) : [],
    error: body.error === null ? null : String(body.error),
    approvalRequired: Boolean(body.approval_required),
    approvalId: body.approval_id === null ? null : String(body.approval_id),
  };
  if (input.surface === "rest_release" && held.approvalId !== null) {
    const revision = Number(built.hub.approvals.find((approval) => approval.id === held.approvalId)?.revision);
    const released = await built.controller.releaseApprovedCommand(held.approvalId, revision);
    expect(released).toBeDefined();
    const observation = fromResult(scenario, released as FanOutResult, built.hub, response.status);
    observation.approval_required = held.approvalRequired;
    observation.approval_id = "approval";
    return observation;
  }
  const observation = fromResult(scenario, held, built.hub, response.status);
  observation.error = held.error;
  return observation;
}

async function executeController(scenario: Scenario): Promise<Observation> {
  const input = scenario.input;
  const built = await buildController(input);
  const result = await built.controller.send(
    input.group.id,
    input.command,
    actor(input.actor),
    sendOptions(input.max_response_ms),
  );
  const status = result.error?.includes("authorization") ? 403 : 200;
  const observation = fromResult(scenario, result, built.hub, status);
  if (result.error?.includes("authorization")) observation.error = "authorization_unavailable";
  return observation;
}

async function executeStore(scenario: Scenario): Promise<Observation> {
  const input = scenario.input;
  const store = new InMemoryFanOutStore();
  const original = fanOutGroup({
    groupId: input.group.id,
    name: "fixture-group",
    workerIds: [...input.group.members],
    createdBy: input.group.creator,
    createdAt: 1,
    grants: [...input.group.grants],
  });
  await store.save(original);
  if (input.operation === "store_atomic_update") {
    const grants = input.concurrent_grants ?? [];
    await Promise.all(grants.map((grant) => store.grantAccess(input.group.id, grant, input.group.creator)));
    const persisted = await store.get(input.group.id);
    expect(persisted?.grants.sort()).toStrictEqual([...input.group.grants, ...grants].sort());
    return base(scenario, 200, input.command, null);
  }
  const mutation = input.mutation_member;
  if (mutation === undefined) throw new Error(`${scenario.id}: store isolation needs mutation_member`);
  original.workerIds.push(mutation);
  original.createdBy = "mutated-creator";
  original.grants.push("mutated-grant");
  const fetched = await store.get(input.group.id);
  expect(fetched?.workerIds).toStrictEqual(input.group.members);
  expect(fetched?.createdBy).toBe(input.group.creator);
  expect(fetched?.grants).toStrictEqual(input.group.grants);
  fetched?.workerIds.push(mutation);
  fetched?.grants.push("mutated-read");
  const listed = await store.listForPrincipal(input.group.creator);
  expect(listed).toHaveLength(1);
  if (listed[0] !== undefined) {
    listed[0].createdBy = "mutated-list";
    listed[0].workerIds.push(mutation);
  }
  expect(await store.get(input.group.id)).toMatchObject({
    workerIds: input.group.members,
    createdBy: input.group.creator,
    grants: input.group.grants,
  });
  return base(scenario, 200, input.command, null);
}

function sendOptions(maxResponseMs: number | undefined): SendOptions {
  return maxResponseMs === undefined ? {} : { maxResponseMs };
}

function routeController(controller: FanOutController): FanoutRoutesController {
  return {
    get allowUnknownMembers() {
      return controller.allowUnknownMembers;
    },
    validateMembers: (workerIds, principal) => controller.validateMembers(workerIds, principal),
    createGroup: (group, principal) => controller.createGroup(group, principal),
    listGroups: (principal) => controller.listGroups(principal),
    getGroup: (groupId, principal) => controller.getGroup(groupId, principal),
    deleteGroup: (groupId, principal) => controller.deleteGroup(groupId, principal),
    grantAccess: (groupId, grantee, principal) => controller.grantAccess(groupId, grantee, principal),
    send: (groupId, data, principal, options) =>
      controller.send(groupId, data, principal, {
        ...(options.quiesceMs === undefined ? {} : { quiesceMs: options.quiesceMs }),
        ...(options.maxResponseMs === undefined ? {} : { maxResponseMs: options.maxResponseMs }),
      }),
  };
}

async function executeScenario(scenario: Scenario): Promise<Observation> {
  if (scenario.input.surface === "rest" || scenario.input.surface === "rest_release") {
    return executeRest(scenario);
  }
  if (scenario.input.surface === "controller") return executeController(scenario);
  if (scenario.input.surface === "store") return executeStore(scenario);
  throw new Error(`TypeScript component does not serve surface ${scenario.input.surface}`);
}

describe("shared fan-out security scenarios", () => {
  it("interprets every applicable scenario input through real component surfaces", async () => {
    const contract = JSON.parse(readFileSync(contractPath, "utf8")) as Contract;
    const applicable = contract.scenarios.filter((scenario) => scenario.backends.typescript.status !== "unserved");
    const observations: Observation[] = [];
    for (const scenario of applicable) observations.push(await executeScenario(scenario));
    expect(observations.map((item) => item.id).sort()).toStrictEqual(applicable.map((item) => item.id).sort());
    if (outputPath === undefined) {
      for (const [index, scenario] of applicable.entries()) {
        const expected = {
          ...scenario.expected,
          ...scenario.backends.typescript.expected,
        };
        const observation = observations[index] as unknown as Record<string, unknown>;
        for (const [field, value] of Object.entries(expected)) {
          expect(observation[field], `${scenario.id}.${field}`).toStrictEqual(value);
        }
      }
    }
    if (outputPath !== undefined) writeFileSync(outputPath, `${JSON.stringify(observations, null, 2)}\n`, "utf8");
  });
});
