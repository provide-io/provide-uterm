//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Fan-out orchestration: one command, many sessions.
 *
 * Port of the Python module
 * `provide.uterm.server.bridge.fanout._controller`.
 *
 * Every path through here answers the same question — who is allowed to drive
 * which fleet — so authorization is checked on each operation rather than
 * once at the door, including again when a held command is finally released.
 */

import { compileExpectRegex } from "../hub/index.ts";
import type { AuthorizablePrincipal } from "../server/authorization.ts";
import type { CollectedOutput, OutputCapture } from "./collector.ts";
import { computeDivergence } from "./divergence.ts";
import {
  type FanOutGroup,
  type FanOutResult,
  type FanOutStore,
  fanOutResult,
  InMemoryFanOutStore,
  type SessionFanOutResult,
} from "./models.ts";

/** How long a held command waits for a decision, in seconds. */
const APPROVAL_WINDOW_S = 300;

/** How much of a command is kept in the audit record. */
const AUDIT_COMMAND_CHARS = 500;

/** A policy decision about a fan-out command. */
export interface FanOutPolicyDecision {
  /** Whether to run it, refuse it, or hold it for approval. */
  action: "allow" | "deny" | "hold";
  /** Why, when it was refused. Surfaced to the caller. */
  reason?: string;
}

/** Gate consulted before a fan-out command runs. */
export interface FanOutPolicyGate {
  /** Decide what to do with `command` for `groupId`. */
  interceptFanout(command: string, context: Record<string, unknown>, groupId: string): Promise<FanOutPolicyDecision>;
}

/** Exact identity of one store generation of an approval id. */
export interface ApprovalIdentity {
  id: string;
  revision: number;
}

/** The hub surface the controller drives. */
export interface FanOutControllerHub {
  /** Deliver a frame to one worker. Returns whether it was accepted. */
  sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean>;
  /** Tell a worker's observers something. */
  broadcast(workerId: string, message: Record<string, unknown>): Promise<void>;
  /** Append to a worker's audit log. */
  appendEvent(workerId: string, eventType: string, data?: Record<string, unknown>): Promise<void>;
  /** Register a command held for approval. */
  addApproval(request: Record<string, unknown>): ApprovalIdentity | undefined;
  /** Open output capture before a worker can emit anything. */
  openOutputCapture?(workerId: string): Promise<OutputCapture | undefined>;
  /** Set by the controller so it hears about approvals that lapse. */
  onApprovalExpired?: ((approval: ApprovalIdentity) => void) | undefined;
}

/** Construction options for {@link FanOutController}. */
export interface FanOutControllerOptions {
  /** Where commands are sent and output is collected. */
  hub: FanOutControllerHub;
  /** Group persistence. Defaults to an in-memory store. */
  store?: FanOutStore;
  /** Most sessions one group may hold. */
  maxGroupSize?: number;
  /** Consulted before every send. Absent means allow. */
  policyGate?: FanOutPolicyGate;
  /** Wall clock in seconds. */
  now?: () => number;
  /** Identifier source for sends and approvals. */
  newId?: () => string;
  /** Resolve the current definition of a group member before delivery. */
  resolveSession?: (workerId: string) => Promise<unknown | undefined>;
  /** Decide whether the full principal is a global administrator. */
  isGlobalAdmin?: (principal: AuthorizablePrincipal) => Promise<boolean>;
  /** Check current session read access for a principal. */
  canReadSession?: (principal: AuthorizablePrincipal, definition: unknown) => Promise<boolean>;
  /** Permit dormant unknown members at group creation. Defaults to strict refusal. */
  allowUnknownMembers?: boolean;
}

/** Per-send overrides for the group's timings. */
export interface SendOptions {
  /** How long a session must be silent before its output is considered done. */
  quiesceMs?: number;
  /** Hard cap on how long to wait for any one session. */
  maxResponseMs?: number;
}

/** A command held awaiting approval. */
interface PendingApproval {
  revision: number;
  groupId: string;
  command: string;
  principal: AuthorizablePrincipal;
  quiesceMs: number | undefined;
  maxResponseMs: number | undefined;
}

/** A session that took the send and what it printed. */
interface Answered {
  index: number;
  output: string;
}

/** Orchestrates fan-out groups and broadcast input. */
export class FanOutController {
  readonly #hub: FanOutControllerHub;
  readonly #store: FanOutStore;
  readonly #maxGroupSize: number;
  readonly #policyGate: FanOutPolicyGate | undefined;
  readonly #now: () => number;
  readonly #newId: () => string;
  readonly #resolveSession: ((workerId: string) => Promise<unknown | undefined>) | undefined;
  readonly #isGlobalAdmin: ((principal: AuthorizablePrincipal) => Promise<boolean>) | undefined;
  readonly #canReadSession: ((principal: AuthorizablePrincipal, definition: unknown) => Promise<boolean>) | undefined;
  readonly #pending = new Map<string, PendingApproval>();
  /** Whether dormant unknown members may be admitted at group creation. */
  readonly allowUnknownMembers: boolean;

  constructor(options: FanOutControllerOptions) {
    this.#hub = options.hub;
    this.#store = options.store ?? new InMemoryFanOutStore();
    this.#maxGroupSize = options.maxGroupSize ?? 50;
    this.#policyGate = options.policyGate;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#newId = options.newId ?? (() => crypto.randomUUID().replaceAll("-", ""));
    this.#resolveSession = options.resolveSession;
    this.#isGlobalAdmin = options.isGlobalAdmin;
    this.#canReadSession = options.canReadSession;
    this.allowUnknownMembers = options.allowUnknownMembers ?? false;
    // A held command that is never decided would otherwise sit in memory for
    // the life of the process, and stay releasable long after its window.
    this.#hub.onApprovalExpired = (approval) => {
      this.#takePending(approval.id, approval.revision);
    };
  }

  /**
   * Validate and store a new group, returning its id.
   *
   * The creator comes from the authenticated principal rather than the
   * submitted record — otherwise anyone could create groups owned by anyone.
   *
   * @throws {RangeError} When the group exceeds the size cap. Every member is
   *   a session one keystroke drives, so the cap is what stops a single
   *   request reaching the whole estate.
   * @throws {PromptRegexError} When the error pattern is unusable. It is
   *   matched against every output delta, so a pathological one is a denial
   *   of service against the fan-out; it is validated here rather than on the
   *   hot path.
   */
  async createGroup(group: FanOutGroup, principal: string): Promise<string> {
    if (group.workerIds.length > this.#maxGroupSize) {
      throw new RangeError(`Group size ${group.workerIds.length} exceeds max ${this.#maxGroupSize}`);
    }
    compileExpectRegex(group.errorPattern);
    group.createdBy = principal;
    await this.#store.save(group);
    return group.groupId;
  }

  /**
   * Every authorizer the controller needs, or undefined when one is missing.
   *
   * Returned together so a caller that has them is holding all three, rather
   * than checking one and reaching for another.
   */
  #authorizers():
    | {
        isGlobalAdmin: (principal: AuthorizablePrincipal) => Promise<boolean>;
        resolveSession: (workerId: string) => Promise<unknown | undefined>;
        canReadSession: (principal: AuthorizablePrincipal, definition: unknown) => Promise<boolean>;
      }
    | undefined {
    const isGlobalAdmin = this.#isGlobalAdmin;
    const resolveSession = this.#resolveSession;
    const canReadSession = this.#canReadSession;
    if (isGlobalAdmin === undefined || resolveSession === undefined || canReadSession === undefined) {
      return undefined;
    }
    return { isGlobalAdmin, resolveSession, canReadSession };
  }

  /**
   * Whether every authorizer the controller needs is wired.
   *
   * A controller missing one cannot judge access at all, so callers refuse
   * rather than proceed on whatever the remaining ones happen to allow.
   */
  get authorizationReady(): boolean {
    return this.#authorizers() !== undefined;
  }

  /**
   * Split members into currently authorized and refused, for `principal`.
   *
   * With no way to resolve sessions or check read access, everything is
   * refused: admission cannot be verified, so nothing is admitted. A resolver
   * or authorizer that throws refuses the member too — failing to decide
   * access is not access.
   *
   * This is the controller's own view, used to narrow a dispatch to the
   * members the caller may still reach — group admission is decided by the
   * routes against the session registry, so that a controller wired to a
   * wider view cannot widen access.
   */
  async validateMembers(workerIds: string[], principal: AuthorizablePrincipal): Promise<[string[], string[]]> {
    const resolveSession = this.#resolveSession;
    const canReadSession = this.#canReadSession;
    if (resolveSession === undefined || canReadSession === undefined) {
      return [[], [...workerIds]];
    }
    const allowed: string[] = [];
    const refused: string[] = [];
    for (const workerId of workerIds) {
      try {
        const definition = await resolveSession(workerId);
        if (definition === undefined || definition === null || !(await canReadSession(principal, definition))) {
          refused.push(workerId);
        } else {
          allowed.push(workerId);
        }
      } catch {
        refused.push(workerId);
      }
    }
    return [allowed, refused];
  }

  /** Delete a group, if `principal` may see it. */
  async deleteGroup(groupId: string, principal: string): Promise<void> {
    if ((await this.#authorized(groupId, principal)) !== undefined) {
      await this.#store.delete(groupId);
    }
  }

  /** The group, if `principal` created it or was granted it. */
  async getGroup(groupId: string, principal: string): Promise<FanOutGroup | undefined> {
    return this.#authorized(groupId, principal);
  }

  /** Every group `principal` may see. */
  async listGroups(principal: string): Promise<FanOutGroup[]> {
    return this.#store.listForPrincipal(principal);
  }

  /**
   * Grant `grantee` access to a group.
   *
   * Only the creator may. A grantee sharing the group onwards would let
   * access spread without the owner ever seeing it.
   */
  async grantAccess(groupId: string, grantee: string, principal: string): Promise<void> {
    await this.#store.grantAccess(groupId, grantee, principal);
  }

  /**
   * Broadcast `data` to a group and collect what each session says.
   *
   * A caller who may not see the group gets an empty result rather than an
   * error: the shape of the answer should not reveal whether it exists.
   */
  async send(
    groupId: string,
    data: string,
    principal: AuthorizablePrincipal | undefined,
    options: SendOptions = {},
  ): Promise<FanOutResult> {
    const authorized = await this.#authorizedMembers(groupId, data, principal);
    if ("error" in authorized) {
      return authorized.error;
    }
    const { group, dispatchGroup, refusedWorkerIds, principal: actor } = authorized;

    const decision = await this.#decide(data, groupId, actor);
    if (decision.action === "deny") {
      return {
        ...this.#emptyResult(groupId, data),
        error: decision.reason ?? "Command blocked by fan-out policy",
      };
    }
    if (decision.action === "hold") {
      return this.#hold(group, data, actor, options);
    }
    return this.#dispatch(group, dispatchGroup, refusedWorkerIds, data, actor, options);
  }

  /**
   * Run a command that was held, once its approval has come through.
   *
   * The pending record is consumed, so a replayed approval id cannot run the
   * command twice. Authorization is checked again: access can be revoked
   * between the hold and the decision, and running it then would honour a
   * permission the sender no longer has.
   */
  async releaseApprovedCommand(requestId: string, revision: number): Promise<FanOutResult | undefined> {
    const pending = this.#takePending(requestId, revision);
    if (pending === undefined) {
      return undefined;
    }
    const authorized = await this.#authorizedMembers(pending.groupId, pending.command, pending.principal);
    if ("error" in authorized) {
      return authorized.error;
    }
    const options: SendOptions = {
      ...(pending.quiesceMs === undefined ? {} : { quiesceMs: pending.quiesceMs }),
      ...(pending.maxResponseMs === undefined ? {} : { maxResponseMs: pending.maxResponseMs }),
    };
    return this.#dispatch(
      authorized.group,
      authorized.dispatchGroup,
      authorized.refusedWorkerIds,
      pending.command,
      pending.principal,
      options,
    );
  }

  /** Consume only the exact generation that supplied this authority. */
  #takePending(requestId: string, revision: number): PendingApproval | undefined {
    const pending = this.#pending.get(requestId);
    if (pending === undefined || pending.revision !== revision) {
      return undefined;
    }
    this.#pending.delete(requestId);
    return pending;
  }

  /** The group, if `principal` created it or was granted it. */
  async #authorized(groupId: string, principal: string): Promise<FanOutGroup | undefined> {
    const group = await this.#store.get(groupId);
    if (group === undefined) {
      return undefined;
    }
    return group.createdBy === principal || group.grants.includes(principal) ? group : undefined;
  }

  async #authorizedMembers(
    groupId: string,
    data: string,
    principal: AuthorizablePrincipal | undefined,
  ): Promise<
    | {
        group: FanOutGroup;
        dispatchGroup: FanOutGroup;
        refusedWorkerIds: string[];
        principal: AuthorizablePrincipal;
      }
    | { error: FanOutResult }
  > {
    if (principal === undefined || typeof principal !== "object" || typeof principal.subject_id !== "string") {
      return { error: this.#errorResult(groupId, data, "authenticated principal required") };
    }
    const authorizers = this.#authorizers();
    if (authorizers === undefined) {
      return { error: this.#errorResult(groupId, data, "fan-out authorization is unavailable") };
    }
    try {
      if (!(await authorizers.isGlobalAdmin(principal))) {
        return { error: this.#errorResult(groupId, data, "global admin role required") };
      }
    } catch {
      return { error: this.#errorResult(groupId, data, "fan-out authorization failed") };
    }
    const group = await this.#store.get(groupId);
    if (group === undefined) {
      return { error: this.#errorResult(groupId, data, "fan-out group not found") };
    }
    if (group.createdBy !== principal.subject_id && !group.grants.includes(principal.subject_id)) {
      return { error: this.#errorResult(groupId, data, "fan-out group not found") };
    }
    const [allowed, refused] = await this.validateMembers(group.workerIds, principal);
    return { group, dispatchGroup: { ...group, workerIds: allowed }, refusedWorkerIds: refused, principal };
  }

  /** Ask the policy gate, defaulting to allow when none is configured. */
  async #decide(data: string, groupId: string, principal: AuthorizablePrincipal): Promise<FanOutPolicyDecision> {
    if (this.#policyGate === undefined) {
      return { action: "allow" };
    }
    return this.#policyGate.interceptFanout(
      data,
      {
        worker_id: `group:${groupId}`,
        client_id: principal.subject_id,
        role: this.#strongestRole(principal),
        action: "fanout_send",
        metadata: { is_fanout: true, group_id: groupId },
      },
      groupId,
    );
  }

  /** Park a command for approval and audit that it was held. */
  async #hold(
    group: FanOutGroup,
    data: string,
    principal: AuthorizablePrincipal,
    options: SendOptions,
  ): Promise<FanOutResult> {
    const requestId = this.#newId();
    const now = this.#now();
    // A held command is a security event, logged whether or not anyone ever
    // approves it. The command is truncated because the audit log is not a
    // transcript store and a caller should not choose how much it writes.
    await this.#hub.appendEvent(`group:${group.groupId}`, "terminal.fanout.hold", {
      group_id: group.groupId,
      command: data.slice(0, AUDIT_COMMAND_CHARS),
      request_id: requestId,
      principal: principal.subject_id,
    });
    const approval = this.#hub.addApproval({
      id: requestId,
      worker_id: `group:${group.groupId}`,
      submitter_id: principal.subject_id,
      command: data,
      status: "pending",
      created_at: now,
      expires_at: now + APPROVAL_WINDOW_S,
      group_id: group.groupId,
      is_fanout: true,
    });
    if (approval === undefined || approval.id !== requestId || !Number.isSafeInteger(approval.revision)) {
      throw new Error("fan-out approval registration failed");
    }
    this.#pending.set(requestId, {
      revision: approval.revision,
      groupId: group.groupId,
      command: data,
      principal,
      quiesceMs: options.quiesceMs,
      maxResponseMs: options.maxResponseMs,
    });

    return {
      ...this.#emptyResult(group.groupId, data, requestId),
      approvalRequired: true,
      approvalId: requestId,
    };
  }

  /** Run the send in whichever mode the group asks for. */
  async #dispatch(
    group: FanOutGroup,
    dispatchGroup: FanOutGroup,
    refusedWorkerIds: string[],
    data: string,
    principal: AuthorizablePrincipal,
    options: SendOptions,
  ): Promise<FanOutResult> {
    const timings = {
      quiesceMs: options.quiesceMs ?? group.quiesceMs,
      maxMs: options.maxResponseMs ?? group.maxResponseMs,
    };
    // Compared against the literal, matching the reference: an unrecognised
    // mode fans out rather than failing closed.
    const result =
      group.mode === "sequential"
        ? await this.#sendSequential(dispatchGroup, data, principal.subject_id, timings)
        : await this.#sendParallel(dispatchGroup, data, principal.subject_id, timings);
    for (const workerId of refusedWorkerIds) {
      if (!result.failedSessions.includes(workerId)) {
        result.results.push(this.#failedRow(workerId));
        result.failedSessions.push(workerId);
      }
    }
    return result;
  }

  /** An outcome carrying no sessions, for the refusal paths. */
  #emptyResult(groupId: string, data: string, sendId?: string): FanOutResult {
    return fanOutResult({
      groupId,
      sendId: sendId ?? this.#newId(),
      command: data,
      sentAt: this.#now(),
      results: [],
      divergentSessions: [],
      failedSessions: [],
    });
  }

  #errorResult(groupId: string, data: string, error: string): FanOutResult {
    return { ...this.#emptyResult(groupId, data), error };
  }

  #strongestRole(principal: AuthorizablePrincipal): "admin" | "operator" | "viewer" {
    for (const role of ["admin", "operator", "viewer"] as const) {
      if (principal.roles.has(role)) {
        return role;
      }
    }
    return "viewer";
  }

  /**
   * Tell every target's observers that this input came from a fan-out.
   *
   * Without it a watcher cannot tell a broadcast from a local hijack, and an
   * operator sees keystrokes they did not type with nothing explaining them.
   * Sent before the command, so the explanation never arrives after the
   * output it explains.
   */
  async #announce(group: FanOutGroup, data: string, sendId: string, principal: string): Promise<void> {
    await Promise.allSettled(
      group.workerIds.map((workerId) =>
        this.#hub.broadcast(workerId, {
          type: "fanout_input",
          group_id: group.groupId,
          send_id: sendId,
          command: data,
          from_principal: principal,
        }),
      ),
    );
  }

  /** A row for a session that never produced output. */
  #failedRow(workerId: string): SessionFanOutResult {
    return { workerId, ok: false, outputDelta: undefined, elapsedMs: 0, divergent: false };
  }

  /** Send to everything at once, then gather from whatever accepted. */
  async #sendParallel(
    group: FanOutGroup,
    data: string,
    principal: string,
    timings: { quiesceMs: number; maxMs: number },
  ): Promise<FanOutResult> {
    const sendId = this.#newId();
    const sentAt = this.#now();
    const frame = { type: "input", data, ts: sentAt };
    const captures: Array<OutputCapture | undefined> = [];
    try {
      for (const workerId of group.workerIds) {
        try {
          const capture = await this.#hub.openOutputCapture?.(workerId);
          captures.push(capture);
        } catch {
          captures.push(undefined);
        }
      }
      const readyIndexes = group.workerIds.flatMap((_, index) => (captures[index] === undefined ? [] : [index]));
      const readyIds = readyIndexes.map((index) => group.workerIds[index] as string);
      await this.#announce({ ...group, workerIds: readyIds }, data, sendId, principal);

      const accepted: Array<{ ok: boolean; startedAt: number } | undefined> = new Array(group.workerIds.length);
      await Promise.all(
        readyIndexes.map(async (index) => {
          const workerId = group.workerIds[index] as string;
          try {
            const ok = await this.#hub.sendWorker(workerId, frame);
            accepted[index] = { ok, startedAt: performance.now() / 1000 };
          } catch {
            accepted[index] = { ok: false, startedAt: performance.now() / 1000 };
          }
        }),
      );

      const collected: Array<CollectedOutput | undefined> = new Array(group.workerIds.length);
      await Promise.all(
        readyIndexes.map(async (index) => {
          const dispatch = accepted[index];
          if (dispatch?.ok !== true) {
            return;
          }
          try {
            collected[index] = await (captures[index] as OutputCapture).collect({
              ...timings,
              startedAt: dispatch.startedAt,
            });
          } catch {
            collected[index] = undefined;
          }
        }),
      );

      const answered: Answered[] = [];
      const results = group.workerIds.map((workerId, index) => {
        if (captures[index] === undefined) {
          return this.#failedRow(workerId);
        }
        const output = collected[index];
        if (output === undefined) {
          return this.#failedRow(workerId);
        }
        answered.push({ index, output: output.output });
        return { workerId, ok: true, outputDelta: output.output, elapsedMs: output.elapsedMs, divergent: false };
      });
      return this.#finish(group, data, sendId, sentAt, results, answered);
    } finally {
      await Promise.allSettled(captures.flatMap((capture) => (capture === undefined ? [] : [capture.close()])));
    }
  }

  /** Send to one session at a time, stopping early if asked to. */
  async #sendSequential(
    group: FanOutGroup,
    data: string,
    principal: string,
    timings: { quiesceMs: number; maxMs: number },
  ): Promise<FanOutResult> {
    const sendId = this.#newId();
    const sentAt = this.#now();
    const frame = { type: "input", data, ts: sentAt };
    const results: SessionFanOutResult[] = [];
    const answered: Answered[] = [];
    let stopped = false;

    for (const workerId of group.workerIds) {
      if (stopped) {
        // Reported without being sent: a bad deploy stops at the first host
        // rather than reaching the last.
        results.push(this.#failedRow(workerId));
        continue;
      }
      let capture: OutputCapture | undefined;
      try {
        capture = await this.#hub.openOutputCapture?.(workerId);
      } catch {
        capture = undefined;
      }
      if (capture === undefined) {
        results.push(this.#failedRow(workerId));
        continue;
      }
      try {
        await this.#announce({ ...group, workerIds: [workerId] }, data, sendId, principal);
        if (!(await this.#hub.sendWorker(workerId, frame))) {
          results.push(this.#failedRow(workerId));
          continue;
        }
        const startedAt = performance.now() / 1000;
        const { output, elapsedMs } = await capture.collect({ ...timings, startedAt });
        answered.push({ index: results.length, output });
        results.push({ workerId, ok: true, outputDelta: output, elapsedMs, divergent: false });

        if (group.stopOnFirstError && group.errorPattern !== undefined) {
          stopped = new RegExp(group.errorPattern).test(output);
        }
      } catch {
        results.push(this.#failedRow(workerId));
      } finally {
        await capture.close();
      }
    }
    return this.#finish(group, data, sendId, sentAt, results, answered);
  }

  /**
   * Judge divergence and assemble the outcome.
   *
   * Only sessions that answered are compared, and the caller passes them in
   * rather than having them re-derived from the rows — a failed session
   * produced nothing, and counting its silence would drag the consensus
   * towards empty and flag the healthy sessions instead.
   */
  #finish(
    group: FanOutGroup,
    data: string,
    sendId: string,
    sentAt: number,
    results: SessionFanOutResult[],
    answered: Answered[],
  ): FanOutResult {
    const divergentSessions: string[] = [];
    if (answered.length > 0) {
      const flags = computeDivergence(
        answered.map((entry) => entry.output),
        group.divergenceThreshold,
      );
      flags.forEach((flag, position) => {
        if (!flag) {
          return;
        }
        const row = results[(answered[position] as Answered).index] as SessionFanOutResult;
        row.divergent = true;
        divergentSessions.push(row.workerId);
      });
    }

    return fanOutResult({
      groupId: group.groupId,
      sendId,
      command: data,
      sentAt,
      results,
      divergentSessions,
      failedSessions: results.filter((row) => !row.ok).map((row) => row.workerId),
    });
  }
}
