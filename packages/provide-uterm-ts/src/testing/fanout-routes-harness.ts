//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Test harness for the fan-out REST routes.
 *
 * The routes sit on a controller, a session registry and the server's
 * authorizer, and every case needs all three wired the way the golden
 * generator wires them — w1..w3 readable, `secret` registered but unreadable,
 * anything else unknown.
 *
 * It lives in `testing/` rather than in a test file because both
 * `routes.test.ts` and `routes-create.test.ts` need it: admission alone is
 * large enough to be its own file, and the two must judge the same fixtures.
 */

import {
  createFanoutRoutes,
  type FanOutGroup,
  type FanOutResult,
  type FanoutRoutesController,
  type FanoutRoutesOptions,
} from "../fanout/index.ts";
import type { AuthorizablePrincipal } from "../server/authorization.ts";
import { loadGolden } from "./golden.ts";

/** The recorded reference behaviour these tests are held against. */
export interface RoutesGolden {
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
  create_authorization_unavailable: { status: number; body: { error: string } };
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

/** The corpus, loaded once for every file that drives these routes. */
export const golden = loadGolden<RoutesGolden>("fanout_routes_golden.json");

/** The caller these fixtures speak for. */
export const PRINCIPAL = "operator@example.org";

/** Somebody else, who created none of it. */
export const OTHER = "intruder@example.org";

/** A controller that records what the routes asked of it. */
export class FakeController implements FanoutRoutesController {
  readonly groups = new Map<string, FanOutGroup>();
  readonly calls: unknown[][] = [];
  createError: unknown;
  sendResult: FanOutResult | undefined;
  allowUnknownMembers = false;
  authorizationReady = true;

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
export function harness(
  options: {
    controller?: FanoutRoutesController;
    readable?: string[];
    allowUnknownMembers?: boolean;
    authorizationReady?: boolean;
  } = {},
) {
  const controller = options.controller ?? new FakeController();
  if (controller instanceof FakeController) {
    controller.allowUnknownMembers = options.allowUnknownMembers ?? controller.allowUnknownMembers;
    controller.authorizationReady = options.authorizationReady ?? controller.authorizationReady;
  }
  // Mirrors the golden generator's fixtures: w1..w3 readable, "secret"
  // registered but unreadable, anything else unknown.
  const readable = new Set(options.readable ?? ["w1", "w2", "w3"]);
  const known = new Set([...readable, "secret"]);
  const audited: Array<{ event: string; principal: string; detail: Record<string, unknown> }> = [];
  /** Every definition read access was decided about, in order. */
  const readChecks: unknown[] = [];
  let counter = 0;
  const deps: FanoutRoutesOptions = {
    controller,
    registry: {
      getDefinition: async (workerId: string) => (known.has(workerId) ? { workerId } : undefined),
    },
    authz: {
      isAdmin: async (principal) => principal.roles.has("admin") && (principal.admin_session_scope ?? null) === null,
      canReadSession: async (_principal, definition) => {
        readChecks.push(definition);
        return readable.has((definition as { workerId: string }).workerId);
      },
    },
    audit: (event, record) => audited.push({ event, ...record }),
    now: () => 1000.0,
    newId: () => {
      counter += 1;
      return `0000000000000000000000000000000${counter}`;
    },
  };
  return { routes: createFanoutRoutes(deps), controller: controller as FakeController, audited, readChecks };
}

/** A request from `principal`, carrying `body`. */
export function request(principal: string, body?: unknown) {
  return {
    principal: { subject_id: principal, roles: new Set(["admin"]), scopes: new Set<string>() },
    ...(body === undefined ? {} : { body }),
  };
}

/** Replace the generated identifier so a response can be compared. */
export function withoutId(body: unknown): unknown {
  return { ...(body as Record<string, unknown>), group_id: "<uuid>" };
}
