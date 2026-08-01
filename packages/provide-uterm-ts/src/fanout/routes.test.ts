//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import type { AuthorizablePrincipal } from "../server/authorization.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  createFanoutRoutes,
  FANOUT_ROUTE_PATHS,
  type FanOutGroup,
  type FanOutResult,
  type FanoutRoutesController,
  type FanoutRoutesOptions,
  fanOutGroup,
} from "./index.ts";

interface RoutesGolden {
  routes: string[];
  disabled: Record<string, { status: number; body: { detail: string } }>;
  create_defaults: {
    response: { status: number; body: Record<string, unknown> };
    group: Record<string, unknown>;
    group_id_length: number;
    group_id_is_hex: boolean;
  };
  create_full: { response: { status: number; body: Record<string, unknown> }; group: Record<string, unknown> };
  create_forbidden: { status: number; body: { error: string } };
  create_unknown_session: { status: number; body: Record<string, unknown> };
  create_rejected: { status: number; body: { error: string } };
  list: { status: number; body: Array<Record<string, unknown>> };
  missing: Record<string, { status: number; body: { error: string } }>;
  not_creator: Record<string, { status: number; body: { error: string } }>;
  send: { status: number; body: Record<string, unknown> };
  send_defaults_call: [string, string, string, string, number | null, number | null];
  grant: { status: number; body: null };
  grant_default_call: [string, string, string, string];
  delete: { status: number; body: null };
  delete_removed_it: boolean;
  audit: Array<{ event: string; principal: string; detail: Record<string, unknown> }>;
  audit_command_length: number;
  malformed_body: Record<string, string | null>;
}

const golden = loadGolden<RoutesGolden>("fanout_routes_golden.json");

const PRINCIPAL = "operator@example.org";
const OTHER = "intruder@example.org";

/** A controller that records what the routes asked of it. */
class FakeController implements FanoutRoutesController {
  readonly groups = new Map<string, FanOutGroup>();
  readonly calls: unknown[][] = [];
  createError: unknown;
  sendResult: FanOutResult | undefined;
  allowUnknownMembers = false;
  /** Mirrors the harness fixtures: w1..w3 readable, anything else refused. */
  readonly readable = new Set(["w1", "w2", "w3"]);

  async validateMembers(workerIds: string[], principal: AuthorizablePrincipal): Promise<[string[], string[]]> {
    this.calls.push(["validate_members", [...workerIds], principal.subject_id]);
    return [
      workerIds.filter((workerId) => this.readable.has(workerId)),
      workerIds.filter((workerId) => !this.readable.has(workerId)),
    ];
  }

  async createGroup(group: FanOutGroup, principal: string): Promise<string> {
    this.calls.push(["create_group", group.groupId, principal]);
    if (this.createError !== undefined) {
      throw this.createError;
    }
    this.groups.set(group.groupId, group);
    return group.groupId;
  }

  async listGroups(principal: string): Promise<FanOutGroup[]> {
    this.calls.push(["list_groups", principal]);
    return [...this.groups.values()];
  }

  async getGroup(groupId: string, principal: string): Promise<FanOutGroup | undefined> {
    this.calls.push(["get_group", groupId, principal]);
    return this.groups.get(groupId);
  }

  async deleteGroup(groupId: string, principal: string): Promise<void> {
    this.calls.push(["delete_group", groupId, principal]);
    this.groups.delete(groupId);
  }

  async grantAccess(groupId: string, grantee: string, principal: string): Promise<void> {
    this.calls.push(["grant_access", groupId, grantee, principal]);
  }

  async send(
    groupId: string,
    data: string,
    principal: AuthorizablePrincipal,
    options: {
      quiesceMs?: number | undefined;
      maxResponseMs?: number | undefined;
    },
  ): Promise<FanOutResult> {
    const call: unknown[] = [
      "send",
      groupId,
      data,
      principal.subject_id,
      options.quiesceMs ?? null,
      options.maxResponseMs ?? null,
    ];
    this.calls.push(call);
    if (this.sendResult !== undefined) {
      return this.sendResult;
    }
    return {
      groupId,
      sendId: "send-1",
      command: data,
      sentAt: 1000.0,
      results: [
        { workerId: "w1", ok: true, outputDelta: "ok", elapsedMs: 12, divergent: false },
        { workerId: "w2", ok: false, outputDelta: undefined, elapsedMs: 34, divergent: true },
      ],
      divergentSessions: ["w2"],
      failedSessions: ["w2"],
      error: null,
      approvalRequired: false,
      approvalId: null,
    };
  }
}

/** The routes, plus everything behind them. */
function harness(
  options: { controller?: FanoutRoutesController; readable?: string[]; allowUnknownMembers?: boolean } = {},
) {
  const controller = options.controller ?? new FakeController();
  if (options.allowUnknownMembers !== undefined && controller instanceof FakeController) {
    controller.allowUnknownMembers = options.allowUnknownMembers;
  }
  const known = new Set([...(options.readable ?? ["w1", "w2", "w3"]), "secret"]);
  const audited: Array<{ event: string; principal: string; detail: Record<string, unknown> }> = [];
  let counter = 0;
  const deps: FanoutRoutesOptions = {
    controller,
    registry: {
      getDefinition: async (workerId: string) => (known.has(workerId) ? { workerId } : undefined),
    },
    authz: {
      isAdmin: async (principal) => principal.roles.has("admin") && (principal.admin_session_scope ?? null) === null,
    },
    audit: (event, record) => audited.push({ event, ...record }),
    now: () => 1000.0,
    newId: () => {
      counter += 1;
      return `0000000000000000000000000000000${counter}`;
    },
  };
  return { routes: createFanoutRoutes(deps), controller: controller as FakeController, audited };
}

/** A request from `principal`, carrying `body`. */
function request(principal: string, body?: unknown) {
  return {
    principal: { subject_id: principal, roles: new Set(["admin"]), scopes: new Set<string>() },
    ...(body === undefined ? {} : { body }),
  };
}

/** Replace the generated identifier so a response can be compared. */
function withoutId(body: unknown): unknown {
  return { ...(body as Record<string, unknown>), group_id: "<uuid>" };
}

describe("the route table", () => {
  it("matches the reference", () => {
    // The paths are the contract every client is written against.
    expect([...FANOUT_ROUTE_PATHS].sort()).toStrictEqual(golden.routes);
  });
});

describe("the global-admin boundary", () => {
  it("fails closed when the admin authorizer is unavailable", async () => {
    const routes = createFanoutRoutes({
      controller: new FakeController(),
      registry: { getDefinition: async () => undefined },
      authz: {
        isAdmin: async () => {
          throw new Error("authorizer unavailable");
        },
      } as FanoutRoutesOptions["authz"],
    });

    const response = await routes.listGroups(request(PRINCIPAL));

    expect(response).toMatchObject({ status: 403, body: { error: "global admin role required" } });
  });

  it("rejects every route before parsing or group lookup", async () => {
    const controller = new FakeController();
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "fleet", workerIds: [], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    const routes = createFanoutRoutes({
      controller,
      registry: { getDefinition: async () => undefined },
      authz: {
        isAdmin: async () => false,
      } as FanoutRoutesOptions["authz"],
    });
    const viewer = {
      subjectId: PRINCIPAL,
      subject_id: PRINCIPAL,
      roles: new Set(["viewer"]),
      scopes: new Set<string>(),
      claims: {},
      tenant_id: null,
    };
    const request = { principal: viewer, body: { data: "id", grantee: OTHER } };

    const responses = [
      await routes.createGroup(request),
      await routes.listGroups(request),
      await routes.deleteGroup(request, "g1"),
      await routes.sendToGroup(request, "g1"),
      await routes.grantAccess(request, "g1"),
    ];

    expect(responses.map((response) => response.status)).toStrictEqual([403, 403, 403, 403, 403]);
    expect(controller.calls).toStrictEqual([]);
  });

  it.each([
    [undefined, 401],
    [{ subject_id: "viewer", roles: new Set(["viewer"]), scopes: new Set<string>() }, 403],
    [{ subject_id: "operator", roles: new Set(["operator"]), scopes: new Set<string>() }, 403],
    [
      {
        subject_id: "scoped-admin",
        roles: new Set(["admin"]),
        scopes: new Set<string>(),
        admin_session_scope: "w1",
      },
      403,
    ],
  ])("rejects %j before an invalid body is parsed", async (principal, expected) => {
    const controller = new FakeController();
    const routes = createFanoutRoutes({
      controller,
      registry: { getDefinition: async () => undefined },
      authz: {
        isAdmin: async (candidate) => candidate.roles.has("admin") && (candidate.admin_session_scope ?? null) === null,
      },
    });

    const response = await routes.createGroup({ principal, body: ["invalid"] });

    expect(response.status).toBe(expected);
    expect(controller.calls).toStrictEqual([]);
  });
});

describe("when the feature is off", () => {
  it("answers 501 on every route", async () => {
    // A 501 rather than a 500 is the difference between "not built" and
    // "broken", and a client can act on the first.
    const { routes } = harness({ controller: undefined as unknown as FanoutRoutesController });
    const off = createFanoutRoutes({
      registry: { getDefinition: async () => undefined },
      authz: { isAdmin: async () => true },
    });
    void routes;
    const responses = [
      await off.createGroup(request(PRINCIPAL, {})),
      await off.listGroups(request(PRINCIPAL)),
      await off.deleteGroup(request(PRINCIPAL), "g1"),
      await off.sendToGroup(request(PRINCIPAL, {}), "g1"),
      await off.grantAccess(request(PRINCIPAL, {}), "g1"),
    ];
    const expected = golden.disabled["GET /api/fanout/groups"];
    for (const response of responses) {
      expect(response).toStrictEqual(expected);
    }
  });
});

describe("creating a group", () => {
  it("fills in every default", async () => {
    const { routes, controller } = harness();
    const response = await routes.createGroup(request(PRINCIPAL, {}));
    expect({ ...response, body: withoutId(response.body) }).toStrictEqual(golden.create_defaults.response);
    const group = [...controller.groups.values()][0] as FanOutGroup;
    expect({
      created_by: group.createdBy,
      divergence_threshold: group.divergenceThreshold,
      error_pattern: group.errorPattern ?? null,
      grants: group.grants,
      max_response_ms: group.maxResponseMs,
      mode: group.mode,
      name: group.name,
      quiesce_ms: group.quiesceMs,
      stop_on_first_error: group.stopOnFirstError,
      worker_ids: group.workerIds,
    }).toStrictEqual(golden.create_defaults.group);
  });

  it("carries every field through when they are given", async () => {
    const { routes, controller } = harness();
    const response = await routes.createGroup(
      request(PRINCIPAL, {
        worker_ids: ["w1", "w2"],
        name: "prod fleet",
        mode: "sequential",
        stop_on_first_error: true,
        error_pattern: "ERROR",
        quiesce_ms: 100,
        max_response_ms: 2000,
        divergence_threshold: 0.5,
      }),
    );
    expect({ ...response, body: withoutId(response.body) }).toStrictEqual(golden.create_full.response);
    const group = [...controller.groups.values()][0] as FanOutGroup;
    expect({
      created_by: group.createdBy,
      divergence_threshold: group.divergenceThreshold,
      error_pattern: group.errorPattern ?? null,
      grants: group.grants,
      max_response_ms: group.maxResponseMs,
      mode: group.mode,
      name: group.name,
      quiesce_ms: group.quiesceMs,
      stop_on_first_error: group.stopOnFirstError,
      worker_ids: group.workerIds,
    }).toStrictEqual(golden.create_full.group);
  });

  it("refuses a session the caller cannot read", async () => {
    // Otherwise a group is a way to reach sessions the caller was never
    // allowed to see.
    const { routes, controller } = harness();
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1", "secret"] }));
    expect(response).toStrictEqual(golden.create_forbidden);
    expect(controller.groups.size).toBe(0);
  });

  it("names the session it refused", async () => {
    expect(golden.create_forbidden.body.error).toContain("secret");
  });

  it("rejects a session it has never heard of by default", async () => {
    const { routes, controller } = harness();
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered"] }));
    expect(response).toStrictEqual({ status: 400, body: { error: "unknown fan-out session: never-registered" } });
    expect(controller.groups.size).toBe(0);
  });

  it("allows dormant members only when explicitly configured", async () => {
    // The gate is the controller's own, not the routes': the corpus records a
    // strict controller refusing an unknown member with a 400 and no group.
    const { routes: strict } = harness();
    const refused = await strict.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered"] }));
    expect(refused.status).toBe(golden.create_unknown_session.status);
    expect((refused.body as Record<string, unknown>).session_count).toBe(
      golden.create_unknown_session.body.session_count,
    );

    const { routes, controller } = harness({ allowUnknownMembers: true });
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered"] }));
    expect(response.status).toBe(200);
    expect((response.body as Record<string, unknown>).session_count).toBe(1);
    expect(controller.groups.size).toBe(1);
  });

  it("still refuses a forbidden member in dormant mode", async () => {
    // The opt-in admits unknown members only; a known-but-unreadable session
    // is refused exactly as before.
    const { routes, controller } = harness({ allowUnknownMembers: true });
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered", "secret"] }));
    expect(response).toStrictEqual({ status: 403, body: { error: "forbidden: no read access to session secret" } });
    expect(controller.groups.size).toBe(0);
  });

  it("names an unknown member ahead of a forbidden one", async () => {
    // The reference classifies the whole refused set before answering, so the
    // unknown-member 400 wins even when a forbidden member comes first.
    const { routes } = harness();
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["secret", "never-registered"] }));
    expect(response).toStrictEqual({ status: 400, body: { error: "unknown fan-out session: never-registered" } });
  });

  it("passes a controller refusal back as a 400", async () => {
    const controller = new FakeController();
    controller.createError = new Error("group too large: 99 > 50");
    const { routes } = harness({ controller });
    expect(await routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1"] }))).toStrictEqual(golden.create_rejected);
  });

  it("lets a failure that is not a refusal through", async () => {
    // A bug in the controller is not a client error, and dressing it as one
    // would send the caller looking at their own request.
    const controller = new FakeController();
    controller.createError = new TypeError("controller is broken");
    const { routes } = harness({ controller });
    await expect(routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1"] }))).rejects.toThrow(TypeError);
  });

  it("gives the group a fresh identifier", async () => {
    const { routes, controller } = harness();
    await routes.createGroup(request(PRINCIPAL, {}));
    await routes.createGroup(request(PRINCIPAL, {}));
    expect(controller.groups.size).toBe(2);
    const [first, second] = [...controller.groups.keys()];
    expect(first).not.toBe(second);
  });

  it("stamps the creator and the time", async () => {
    const { routes, controller } = harness();
    await routes.createGroup(request(PRINCIPAL, {}));
    const group = [...controller.groups.values()][0] as FanOutGroup;
    expect(group.createdBy).toBe(PRINCIPAL);
    expect(group.createdAt).toBe(1000.0);
  });
});

describe("a body that is not an object", () => {
  it("fails rather than applying every default", async () => {
    // The reference reaches for `.get` on whatever arrives, so a list raises
    // there. Reading it as an empty object would accept a request the
    // reference rejects — with an empty group, or an empty command.
    const { routes, controller } = harness();
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "", workerIds: ["w1"], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    expect(Object.keys(golden.malformed_body).every((key) => golden.malformed_body[key] !== null)).toBe(true);
    await expect(routes.createGroup(request(PRINCIPAL, ["not", "an", "object"]))).rejects.toThrow(TypeError);
    await expect(routes.sendToGroup(request(PRINCIPAL, "a string"), "g1")).rejects.toThrow(TypeError);
    await expect(routes.grantAccess(request(PRINCIPAL, 42), "g1")).rejects.toThrow(TypeError);
  });

  it("does not affect the routes that take no body", async () => {
    const { routes, controller } = harness();
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "", workerIds: [], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    expect((await routes.listGroups(request(PRINCIPAL))).status).toBe(200);
    expect((await routes.deleteGroup(request(PRINCIPAL), "g1")).status).toBe(204);
  });
});

describe("listing groups", () => {
  it("returns only the summary fields", async () => {
    // The full record carries grants and thresholds, which a listing has no
    // reason to hand out.
    const { routes, controller } = harness();
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "fleet", workerIds: ["w1", "w2"], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    const response = await routes.listGroups(request(PRINCIPAL));
    expect(response.status).toBe(golden.list.status);
    expect(response.body).toStrictEqual([{ group_id: "g1", name: "fleet", session_count: 2, mode: "parallel" }]);
  });

  it("asks the controller for the caller's groups", async () => {
    const { routes, controller } = harness();
    await routes.listGroups(request(PRINCIPAL));
    expect(controller.calls.at(-1)).toStrictEqual(["list_groups", PRINCIPAL]);
  });

  it("is empty when there are none", async () => {
    const { routes } = harness();
    expect((await routes.listGroups(request(PRINCIPAL))).body).toStrictEqual([]);
  });
});

describe("a group that is not there", () => {
  /** Every route that takes a group id. */
  async function attempts(routes: ReturnType<typeof harness>["routes"], principal: string) {
    return {
      delete: await routes.deleteGroup(request(principal), "nope"),
      send: await routes.sendToGroup(request(principal, { data: "ls\r" }), "nope"),
      grant: await routes.grantAccess(request(principal, { grantee: OTHER }), "nope"),
    };
  }

  it("answers 404 on every route that names one", async () => {
    const { routes } = harness();
    expect(await attempts(routes, PRINCIPAL)).toStrictEqual(golden.missing);
  });

  it("answers 404 rather than 403 for a group the caller has no part in", async () => {
    // 403 would confirm the group exists, which is a probe.
    const { routes } = harness();
    expect((await attempts(routes, OTHER)).delete.status).toBe(404);
  });

  it("looks the group up as the caller, so one it may not see is simply absent", async () => {
    // This is what makes the 404 real: an access-aware controller hides the
    // group, and the route never gets far enough to answer 403. Looking it up
    // without the principal would turn every hidden group into a 403, which
    // is a yes/no oracle for whether it exists.
    const controller = new FakeController();
    const hidden = fanOutGroup({
      groupId: "g1",
      name: "",
      workerIds: [],
      createdBy: PRINCIPAL,
      createdAt: 1,
    });
    controller.groups.set("g1", hidden);
    controller.getGroup = async (groupId: string, principal: string) =>
      principal === PRINCIPAL ? controller.groups.get(groupId) : undefined;
    const { routes } = harness({ controller });
    expect(await routes.deleteGroup(request(OTHER), "g1")).toStrictEqual(golden.missing.delete);
    expect(await routes.sendToGroup(request(OTHER, { data: "ls" }), "g1")).toStrictEqual(golden.missing.send);
    expect(await routes.grantAccess(request(OTHER, { grantee: OTHER }), "g1")).toStrictEqual(golden.missing.grant);
  });
});

describe("a group the caller did not create", () => {
  /** A group created by `PRINCIPAL`. */
  function existing(controller: FakeController): string {
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "fleet", workerIds: ["w1"], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    return "g1";
  }

  it("may not be deleted", async () => {
    const { routes, controller } = harness();
    const groupId = existing(controller);
    expect(await routes.deleteGroup(request(OTHER), groupId)).toStrictEqual(golden.not_creator.delete);
    expect(controller.groups.has(groupId)).toBe(true);
  });

  it("may not be granted away", async () => {
    // Being able to send to a group is not being able to hand it to somebody
    // else.
    const { routes, controller } = harness();
    const groupId = existing(controller);
    expect(await routes.grantAccess(request(OTHER, { grantee: OTHER }), groupId)).toStrictEqual(
      golden.not_creator.grant,
    );
    expect(controller.calls.some((call) => call[0] === "grant_access")).toBe(false);
  });

  it("may still be sent to", async () => {
    // Sending is what a grant is for; only the group's shape is the
    // creator's to change.
    const { routes, controller } = harness();
    const groupId = existing(controller);
    expect((await routes.sendToGroup(request(OTHER, { data: "ls\r" }), groupId)).status).toBe(200);
  });
});

describe("sending to a group", () => {
  /** A harness holding one group. */
  function withGroup() {
    const harnessed = harness();
    harnessed.controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "", workerIds: ["w1", "w2"], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    return harnessed;
  }

  it("shapes the result the way the reference does", async () => {
    const { routes } = withGroup();
    const response = await routes.sendToGroup(
      request(PRINCIPAL, { data: "uptime\r", quiesce_ms: 50, max_response_ms: 900 }),
      "g1",
    );
    expect(response.status).toBe(golden.send.status);
    expect({ ...(response.body as Record<string, unknown>), group_id: "<uuid>" }).toStrictEqual({
      ...golden.send.body,
      group_id: "<uuid>",
      error: null,
      approval_required: false,
      approval_id: null,
      command: "uptime\r",
    });
  });

  it.each([
    ["deny", "blocked", false, null],
    ["hold", null, true, "approval-1"],
  ])("serializes explicit policy fields for %s", async (_kind, error, approvalRequired, approvalId) => {
    const { routes, controller } = withGroup();
    controller.sendResult = {
      groupId: "g1",
      sendId: approvalId ?? "send-1",
      command: "id",
      sentAt: 1000,
      results: [],
      divergentSessions: [],
      failedSessions: [],
      error,
      approvalRequired,
      approvalId,
    };

    const response = await routes.sendToGroup(request(PRINCIPAL, { data: "id" }), "g1");

    expect(response.status).toBe(200);
    expect(response.body).toMatchObject({
      error,
      approval_required: approvalRequired,
      approval_id: approvalId,
    });
  });

  it("passes the caller's timing overrides through", async () => {
    const { routes, controller } = withGroup();
    await routes.sendToGroup(request(PRINCIPAL, { data: "x", quiesce_ms: 50, max_response_ms: 900 }), "g1");
    expect(controller.calls.at(-1)).toStrictEqual(["send", "g1", "x", PRINCIPAL, 50, 900]);
  });

  it("leaves the timings unset when the caller gives none", async () => {
    // Unset means "use the group's own", which is not the same as zero.
    const { routes, controller } = withGroup();
    await routes.sendToGroup(request(PRINCIPAL, {}), "g1");
    expect(controller.calls.at(-1)).toStrictEqual([
      "send",
      "g1",
      golden.send_defaults_call[2],
      PRINCIPAL,
      golden.send_defaults_call[4],
      golden.send_defaults_call[5],
    ]);
  });

  it("leaves current member authorization to the controller", async () => {
    const harnessed = harness({ readable: ["w1"] });
    harnessed.controller.groups.set(
      "g1",
      fanOutGroup({
        groupId: "g1",
        name: "private-member",
        workerIds: ["w1", "secret"],
        createdBy: PRINCIPAL,
        createdAt: 1000,
      }),
    );

    await harnessed.routes.sendToGroup(request(PRINCIPAL, { data: "id" }), "g1");

    expect(harnessed.controller.calls.at(-1)).toStrictEqual(["send", "g1", "id", PRINCIPAL, null, null]);
  });
});

describe("granting access", () => {
  /** A harness holding one group. */
  function withGroup() {
    const harnessed = harness();
    harnessed.controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "", workerIds: ["w1"], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    return harnessed;
  }

  it("answers with no content", async () => {
    const { routes } = withGroup();
    expect(await routes.grantAccess(request(PRINCIPAL, { grantee: OTHER }), "g1")).toStrictEqual(golden.grant);
  });

  it("passes the grantee through", async () => {
    const { routes, controller } = withGroup();
    await routes.grantAccess(request(PRINCIPAL, { grantee: OTHER }), "g1");
    expect(controller.calls.at(-1)).toStrictEqual(["grant_access", "g1", OTHER, PRINCIPAL]);
  });

  it("defaults an absent grantee to nobody rather than everybody", async () => {
    const { routes, controller } = withGroup();
    await routes.grantAccess(request(PRINCIPAL, {}), "g1");
    expect(controller.calls.at(-1)).toStrictEqual(["grant_access", "g1", golden.grant_default_call[2], PRINCIPAL]);
  });
});

describe("deleting a group", () => {
  it("answers with no content and removes it", async () => {
    const { routes, controller } = harness();
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "", workerIds: [], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    expect(await routes.deleteGroup(request(PRINCIPAL), "g1")).toStrictEqual(golden.delete);
    expect(controller.groups.has("g1")).toBe(!golden.delete_removed_it);
  });
});

describe("the audit trail", () => {
  it("records every change, and nothing that only reads", async () => {
    // The trail is what an incident review has; a create, a send, a grant and
    // a delete all change what a fleet can be driven with.
    const { routes, controller, audited } = harness();
    const created = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1"], name: "fleet" }));
    const groupId = (created.body as { group_id: string }).group_id;
    await routes.listGroups(request(PRINCIPAL));
    await routes.sendToGroup(request(PRINCIPAL, { data: "ls\r" }), groupId);
    await routes.grantAccess(request(PRINCIPAL, { grantee: OTHER }), groupId);
    await routes.deleteGroup(request(PRINCIPAL), groupId);
    expect(audited.map((record) => record.event)).toStrictEqual([
      "fanout.create_group",
      "fanout.send",
      "fanout.grant_access",
      "fanout.delete_group",
    ]);
    expect(audited.every((record) => record.principal === PRINCIPAL)).toBe(true);
    void controller;
  });

  it("records nothing for a request it refused", async () => {
    const { routes, audited } = harness();
    await routes.createGroup(request(PRINCIPAL, { worker_ids: ["secret"] }));
    await routes.deleteGroup(request(PRINCIPAL), "nope");
    expect(audited).toStrictEqual([]);
  });

  it("truncates the command it records", async () => {
    // The audit trail is not a transcript, and a paste of a whole script
    // would bury the events either side of it.
    const { routes, controller, audited } = harness();
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "", workerIds: ["w1"], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    await routes.sendToGroup(request(PRINCIPAL, { data: "x".repeat(300) }), "g1");
    expect((audited.at(-1)?.detail.command as string).length).toBe(golden.audit_command_length);
  });

  it("matches the recorded details", async () => {
    const { routes, controller, audited } = harness();
    controller.groups.set(
      "g1",
      fanOutGroup({ groupId: "g1", name: "", workerIds: ["w1"], createdBy: PRINCIPAL, createdAt: 1 }),
    );
    await routes.grantAccess(request(PRINCIPAL, { grantee: OTHER }), "g1");
    const recorded = golden.audit.find((record) => record.event === "fanout.grant_access");
    expect(audited.at(-1)?.detail).toStrictEqual({ ...recorded?.detail, group_id: "g1" });
  });

  it("works without an audit sink at all", async () => {
    // The sink is optional; a deployment without one must not fail every
    // write.
    const routes = createFanoutRoutes({
      controller: new FakeController(),
      registry: { getDefinition: async () => undefined },
      authz: { isAdmin: async () => true },
    });
    expect((await routes.createGroup(request(PRINCIPAL, {}))).status).toBe(200);
  });
});
