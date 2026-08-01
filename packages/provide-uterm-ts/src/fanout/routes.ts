//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * REST routes for the fan-out feature.
 *
 * Port of the Python module `provide.uterm.server.bridge.fanout._routes`.
 *
 * Fan-out is one command typed at many production sessions at once, so this
 * surface is where the blast radius is decided:
 *
 * - A group may only contain sessions the creator can already read, checked
 *   per session at creation time. Otherwise a group is a way to reach
 *   sessions the caller was never allowed to see.
 * - Only the creator may delete a group or grant access to it. Being able to
 *   *send* to a group is not being able to hand it to somebody else.
 * - The status codes are the contract: 404 before 403, so a caller cannot
 *   probe for groups it has no part in.
 */

import type { AuthorizablePrincipal } from "../server/authorization.ts";
import { type FanOutGroup, type FanOutMode, type FanOutResult, fanOutGroup } from "./models.ts";

/** A request, as much of one as these handlers read. */
export interface FanoutRequest {
  /** Who is asking. */
  principal?: AuthorizablePrincipal | undefined;
  /** The parsed JSON body, when the route takes one. */
  body?: unknown;
}

/** What a handler answers with. */
export interface RouteResponse {
  /** HTTP status. */
  status: number;
  /** The JSON body, null where there is nothing to say. */
  body?: unknown;
}

/** The controller surface the routes drive. */
export interface FanoutRoutesController {
  /** Whether dormant unknown members may be admitted at group creation. */
  readonly allowUnknownMembers: boolean;
  /** Split members into currently authorized and refused, for `principal`. */
  validateMembers(workerIds: string[], principal: AuthorizablePrincipal): Promise<[string[], string[]]>;
  /** Register a group and return its identifier. */
  createGroup(group: FanOutGroup, principal: string): Promise<string>;
  /** The groups the caller may see. */
  listGroups(principal: string): Promise<FanOutGroup[]>;
  /** One group, if the caller may see it. */
  getGroup(groupId: string, principal: string): Promise<FanOutGroup | undefined>;
  /** Forget a group. */
  deleteGroup(groupId: string, principal: string): Promise<void>;
  /** Let another principal use a group. */
  grantAccess(groupId: string, grantee: string, principal: string): Promise<void>;
  /** Broadcast to a group. */
  send(
    groupId: string,
    data: string,
    principal: AuthorizablePrincipal,
    options: {
      quiesceMs?: number | undefined;
      maxResponseMs?: number | undefined;
    },
  ): Promise<FanOutResult>;
}

/** What the routes need to answer a request. */
export interface FanoutRoutesOptions {
  /** Absent means the feature is switched off, and every route answers 501. */
  controller?: FanoutRoutesController | undefined;
  /** Where a session's definition is looked up, to tell unknown from forbidden. */
  registry: { getDefinition(workerId: string): Promise<unknown> };
  /** Whether the caller is a global administrator. */
  authz: {
    isAdmin(principal: AuthorizablePrincipal): Promise<boolean>;
  };
  /** Where changes are recorded. Optional: a deployment may have no sink. */
  audit?: (event: string, record: { principal: string; detail: Record<string, unknown> }) => void;
  /** Wall clock in seconds. */
  now?: () => number;
  /** Identifier source for new groups. */
  newId?: () => string;
}

/** The handlers, one per route. */
export interface FanoutRoutes {
  /** `POST /api/fanout/groups` */
  createGroup(request: FanoutRequest): Promise<RouteResponse>;
  /** `GET /api/fanout/groups` */
  listGroups(request: FanoutRequest): Promise<RouteResponse>;
  /** `DELETE /api/fanout/groups/{group_id}` */
  deleteGroup(request: FanoutRequest, groupId: string): Promise<RouteResponse>;
  /** `POST /api/fanout/groups/{group_id}/send` */
  sendToGroup(request: FanoutRequest, groupId: string): Promise<RouteResponse>;
  /** `POST /api/fanout/groups/{group_id}/grants` */
  grantAccess(request: FanoutRequest, groupId: string): Promise<RouteResponse>;
}

/** The paths these handlers serve — the contract clients are written against. */
export const FANOUT_ROUTE_PATHS: readonly string[] = [
  "DELETE /api/fanout/groups/{group_id}",
  "GET /api/fanout/groups",
  "POST /api/fanout/groups",
  "POST /api/fanout/groups/{group_id}/grants",
  "POST /api/fanout/groups/{group_id}/send",
];

/** How much of a command reaches the audit record. */
const AUDIT_COMMAND_CHARS = 120;

/** How long a generated group identifier is. */
const GROUP_ID_CHARS = 32;

/** Group defaults, applied when the body leaves a field out. */
const DEFAULTS = {
  mode: "parallel" as FanOutMode,
  stopOnFirstError: false,
  quiesceMs: 500,
  maxResponseMs: 10_000,
  divergenceThreshold: 0.8,
};

/**
 * A body's field, when it is of the expected shape.
 *
 * A body that is not an object at all is a failure, not an empty one: the
 * reference reaches for `.get` on whatever arrives, so a list or a string
 * raises there. Quietly reading it as `{}` would accept a request the
 * reference rejects, and every default would silently apply.
 */
function field<T>(body: unknown, name: string, guard: (value: unknown) => value is T): T | undefined {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new TypeError("request body must be a JSON object");
  }
  const value = (body as Record<string, unknown>)[name];
  return guard(value) ? value : undefined;
}

/** Whether a value is a string. */
function isString(value: unknown): value is string {
  return typeof value === "string";
}

/** Whether a value is a number. */
function isNumber(value: unknown): value is number {
  return typeof value === "number";
}

/** Whether a value is a boolean. */
function isBoolean(value: unknown): value is boolean {
  return typeof value === "boolean";
}

/** Whether a value is an array of strings. */
function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString);
}

/** A hex identifier, the shape the reference's `uuid4().hex` produces. */
function randomId(): string {
  let out = "";
  while (out.length < GROUP_ID_CHARS) {
    out += Math.floor(Math.random() * 0x10000)
      .toString(16)
      .padStart(4, "0");
  }
  return out.slice(0, GROUP_ID_CHARS);
}

/** A refusal, in the reference's shape. */
function refusal(status: number, message: string): RouteResponse {
  return { status, body: { error: message } };
}

/** Build the fan-out route handlers. */
export function createFanoutRoutes(options: FanoutRoutesOptions): FanoutRoutes {
  const now = options.now ?? (() => Date.now() / 1000);
  const newId = options.newId ?? randomId;
  const audit = options.audit;

  /**
   * The controller, or the response to send instead.
   *
   * A 501 rather than a 500 is the difference between "not built" and
   * "broken", and a client can act on the first.
   */
  const controller = (): FanoutRoutesController | RouteResponse =>
    options.controller ?? { status: 501, body: { detail: "fan-out feature is not enabled" } };

  /** Whether the disabled response came back instead of a controller. */
  const isDisabled = (candidate: FanoutRoutesController | RouteResponse): candidate is RouteResponse =>
    (candidate as RouteResponse).status !== undefined;

  /** Record a change, where there is somewhere to record it. */
  const record = (event: string, principal: string, detail: Record<string, unknown>): void => {
    audit?.(event, { principal, detail });
  };

  const authorize = async (request: FanoutRequest): Promise<AuthorizablePrincipal | RouteResponse> => {
    const principal = request.principal;
    if (principal === undefined || principal.subject_id === "anonymous") {
      return refusal(401, "authentication required");
    }
    try {
      if (!(await options.authz.isAdmin(principal))) {
        return refusal(403, "global admin role required");
      }
    } catch {
      return refusal(403, "global admin role required");
    }
    return principal;
  };

  const isRefusal = (candidate: AuthorizablePrincipal | RouteResponse): candidate is RouteResponse =>
    (candidate as RouteResponse).status !== undefined;

  return {
    async createGroup(request: FanoutRequest): Promise<RouteResponse> {
      const authorized = await authorize(request);
      if (isRefusal(authorized)) {
        return authorized;
      }
      const ctrl = controller();
      if (isDisabled(ctrl)) {
        return ctrl;
      }
      const principal = authorized.subject_id;
      const body = request.body;
      const workerIds = field(body, "worker_ids", isStringArray) ?? [];
      const name = field(body, "name", isString) ?? "";

      // Admission is the controller's call: it classifies every member, and
      // the registry then splits the refused into unknown and forbidden.
      // Strict by default — dormant members require the controller's explicit
      // opt-in, while every known member always requires current read access.
      const [, refused] = await ctrl.validateMembers([...workerIds], authorized);
      if (refused.length > 0) {
        const unknown: string[] = [];
        for (const workerId of refused) {
          const definition = await options.registry.getDefinition(workerId);
          if (definition === undefined || definition === null) {
            unknown.push(workerId);
          }
        }
        if (unknown.length > 0 && !ctrl.allowUnknownMembers) {
          return refusal(400, `unknown fan-out session: ${unknown[0]}`);
        }
        const forbidden = refused.filter((workerId) => !unknown.includes(workerId));
        if (forbidden.length > 0) {
          return refusal(403, `forbidden: no read access to session ${forbidden[0]}`);
        }
        // What remains is unknown members under an explicit dormant-member
        // opt-in, which is the one refusal that may proceed.
      }

      const errorPattern = field(body, "error_pattern", isString);
      const group = fanOutGroup({
        groupId: newId(),
        name,
        workerIds: [...workerIds],
        createdBy: principal,
        createdAt: now(),
        mode: (field(body, "mode", isString) ?? DEFAULTS.mode) as FanOutMode,
        stopOnFirstError: field(body, "stop_on_first_error", isBoolean) ?? DEFAULTS.stopOnFirstError,
        ...(errorPattern === undefined ? {} : { errorPattern }),
        quiesceMs: field(body, "quiesce_ms", isNumber) ?? DEFAULTS.quiesceMs,
        maxResponseMs: field(body, "max_response_ms", isNumber) ?? DEFAULTS.maxResponseMs,
        divergenceThreshold: field(body, "divergence_threshold", isNumber) ?? DEFAULTS.divergenceThreshold,
      });

      let groupId: string;
      try {
        groupId = await ctrl.createGroup(group, principal);
      } catch (thrown) {
        // A refusal is a client error; a bug in the controller is not, and
        // dressing one as a 400 would send the caller looking at their own
        // request.
        if (thrown instanceof TypeError || thrown instanceof RangeError) {
          throw thrown;
        }
        return refusal(400, (thrown as Error).message);
      }
      record("fanout.create_group", principal, { group_id: groupId, name });
      return { status: 200, body: { group_id: groupId, name, session_count: workerIds.length } };
    },

    async listGroups(request: FanoutRequest): Promise<RouteResponse> {
      const authorized = await authorize(request);
      if (isRefusal(authorized)) {
        return authorized;
      }
      const ctrl = controller();
      if (isDisabled(ctrl)) {
        return ctrl;
      }
      const groups = await ctrl.listGroups(authorized.subject_id);
      // Summary fields only: the full record carries grants and thresholds,
      // which a listing has no reason to hand out.
      return {
        status: 200,
        body: groups.map((group) => ({
          group_id: group.groupId,
          name: group.name,
          session_count: group.workerIds.length,
          mode: group.mode,
        })),
      };
    },

    async deleteGroup(request: FanoutRequest, groupId: string): Promise<RouteResponse> {
      const authorized = await authorize(request);
      if (isRefusal(authorized)) {
        return authorized;
      }
      const ctrl = controller();
      if (isDisabled(ctrl)) {
        return ctrl;
      }
      const principal = authorized.subject_id;
      // Looked up *as the caller*: an access-aware controller hides a group
      // the caller has no part in, so it comes back absent and the 403 below
      // is never reached. Swapping the two checks here changes nothing on its
      // own — that lookup is what makes the 404 real.
      const existing = await ctrl.getGroup(groupId, principal);
      if (existing === undefined) {
        return refusal(404, "group not found");
      }
      if (existing.createdBy !== principal) {
        return refusal(403, "only the group creator can delete it");
      }
      await ctrl.deleteGroup(groupId, principal);
      record("fanout.delete_group", principal, { group_id: groupId });
      return { status: 204, body: null };
    },

    async sendToGroup(request: FanoutRequest, groupId: string): Promise<RouteResponse> {
      const authorized = await authorize(request);
      if (isRefusal(authorized)) {
        return authorized;
      }
      const ctrl = controller();
      if (isDisabled(ctrl)) {
        return ctrl;
      }
      const principal = authorized.subject_id;
      const existing = await ctrl.getGroup(groupId, principal);
      if (existing === undefined) {
        return refusal(404, "group not found");
      }
      const body = request.body;
      const data = field(body, "data", isString) ?? "";
      // Left unset rather than defaulted: unset means "use the group's own",
      // which is not the same as zero.
      const result = await ctrl.send(groupId, data, authorized, {
        quiesceMs: field(body, "quiesce_ms", isNumber),
        maxResponseMs: field(body, "max_response_ms", isNumber),
      });
      record("fanout.send", principal, {
        group_id: groupId,
        send_id: result.sendId,
        // The audit trail is not a transcript: a pasted script would bury the
        // events either side of it.
        command: data.slice(0, AUDIT_COMMAND_CHARS),
      });
      return {
        status: 200,
        body: {
          group_id: result.groupId,
          send_id: result.sendId,
          command: result.command,
          sent_at: result.sentAt,
          results: result.results.map((session) => ({
            worker_id: session.workerId,
            ok: session.ok,
            output_delta: session.outputDelta ?? null,
            elapsed_ms: session.elapsedMs,
            divergent: session.divergent,
          })),
          divergent_sessions: result.divergentSessions,
          failed_sessions: result.failedSessions,
          error: result.error,
          approval_required: result.approvalRequired,
          approval_id: result.approvalId,
        },
      };
    },

    async grantAccess(request: FanoutRequest, groupId: string): Promise<RouteResponse> {
      const authorized = await authorize(request);
      if (isRefusal(authorized)) {
        return authorized;
      }
      const ctrl = controller();
      if (isDisabled(ctrl)) {
        return ctrl;
      }
      const principal = authorized.subject_id;
      const existing = await ctrl.getGroup(groupId, principal);
      if (existing === undefined) {
        return refusal(404, "group not found");
      }
      if (existing.createdBy !== principal) {
        return refusal(403, "only the group creator can grant access");
      }
      const grantee = field(request.body, "grantee", isString) ?? "";
      await ctrl.grantAccess(groupId, grantee, principal);
      record("fanout.grant_access", principal, { group_id: groupId, grantee });
      return { status: 204, body: null };
    },
  };
}
