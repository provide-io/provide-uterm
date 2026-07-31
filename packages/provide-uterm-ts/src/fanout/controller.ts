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

/** The hub surface the controller drives. */
export interface FanOutControllerHub {
  /** Deliver a frame to one worker. Returns whether it was accepted. */
  sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean>;
  /** Tell a worker's observers something. */
  broadcast(workerId: string, message: Record<string, unknown>): Promise<void>;
  /** Append to a worker's audit log. */
  appendEvent(workerId: string, eventType: string, data?: Record<string, unknown>): Promise<void>;
  /** Register a command held for approval. */
  addApproval(request: Record<string, unknown>): void;
  /** Gather what a worker printed after a send. */
  collectOutput(
    workerId: string,
    options: { quiesceMs: number; maxMs: number },
  ): Promise<{ output: string; elapsedMs: number }>;
  /** Set by the controller so it hears about approvals that lapse. */
  onApprovalExpired?: ((requestId: string) => void) | undefined;
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
  /** Check current session read access for a principal. */
  canReadSession?: (principal: string, definition: unknown) => Promise<boolean>;
}

/** Per-send overrides for the group's timings. */
export interface SendOptions {
  /** How long a session must be silent before its output is considered done. */
  quiesceMs?: number;
  /** Hard cap on how long to wait for any one session. */
  maxResponseMs?: number;
  /** Route-authorized members. Internal route/controller integration option. */
  memberWorkerIds?: string[];
  /** Members refused by current route authorization. */
  refusedWorkerIds?: string[];
}

/** A command held awaiting approval. */
interface PendingApproval {
  groupId: string;
  command: string;
  principal: string;
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
  readonly #canReadSession: ((principal: string, definition: unknown) => Promise<boolean>) | undefined;
  readonly #pending = new Map<string, PendingApproval>();

  constructor(options: FanOutControllerOptions) {
    this.#hub = options.hub;
    this.#store = options.store ?? new InMemoryFanOutStore();
    this.#maxGroupSize = options.maxGroupSize ?? 50;
    this.#policyGate = options.policyGate;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#newId = options.newId ?? (() => crypto.randomUUID().replaceAll("-", ""));
    this.#resolveSession = options.resolveSession;
    this.#canReadSession = options.canReadSession;
    // A held command that is never decided would otherwise sit in memory for
    // the life of the process, and stay releasable long after its window.
    this.#hub.onApprovalExpired = (requestId) => {
      this.#pending.delete(requestId);
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
    const group = await this.#store.get(groupId);
    if (group === undefined || group.createdBy !== principal) {
      return;
    }
    if (!group.grants.includes(grantee)) {
      group.grants.push(grantee);
      await this.#store.save(group);
    }
  }

  /**
   * Broadcast `data` to a group and collect what each session says.
   *
   * A caller who may not see the group gets an empty result rather than an
   * error: the shape of the answer should not reveal whether it exists.
   */
  async send(groupId: string, data: string, principal: string, options: SendOptions = {}): Promise<FanOutResult> {
    const group = await this.#authorized(groupId, principal);
    if (group === undefined) {
      return this.#emptyResult(groupId, data);
    }

    const decision = await this.#decide(data, groupId, principal);
    if (decision.action === "deny") {
      return {
        ...this.#emptyResult(groupId, data),
        error: decision.reason ?? "Command blocked by fan-out policy",
      };
    }
    if (decision.action === "hold") {
      return this.#hold(group, data, principal, options);
    }
    const authorized = await this.#authorizedMembers(group, principal, options);
    return this.#dispatch(group, data, principal, { ...options, ...authorized });
  }

  /**
   * Run a command that was held, once its approval has come through.
   *
   * The pending record is consumed, so a replayed approval id cannot run the
   * command twice. Authorization is checked again: access can be revoked
   * between the hold and the decision, and running it then would honour a
   * permission the sender no longer has.
   */
  async releaseApprovedCommand(requestId: string): Promise<FanOutResult | undefined> {
    const pending = this.#pending.get(requestId);
    if (pending === undefined) {
      return undefined;
    }
    this.#pending.delete(requestId);
    const group = await this.#authorized(pending.groupId, pending.principal);
    if (group === undefined) {
      return undefined;
    }
    const options: SendOptions = {
      ...(pending.quiesceMs === undefined ? {} : { quiesceMs: pending.quiesceMs }),
      ...(pending.maxResponseMs === undefined ? {} : { maxResponseMs: pending.maxResponseMs }),
    };
    const authorized = await this.#authorizedMembers(group, pending.principal, options);
    return this.#dispatch(group, pending.command, pending.principal, { ...options, ...authorized });
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
    group: FanOutGroup,
    principal: string,
    options: SendOptions,
  ): Promise<Pick<SendOptions, "memberWorkerIds" | "refusedWorkerIds">> {
    if (this.#resolveSession === undefined || this.#canReadSession === undefined) {
      return {
        memberWorkerIds: options.memberWorkerIds ?? [...group.workerIds],
        refusedWorkerIds: options.refusedWorkerIds ?? [],
      };
    }
    const routeAllowed = new Set(options.memberWorkerIds ?? group.workerIds);
    const refused = new Set(options.refusedWorkerIds ?? []);
    const allowed: string[] = [];
    for (const workerId of group.workerIds) {
      if (!routeAllowed.has(workerId)) {
        refused.add(workerId);
        continue;
      }
      try {
        const definition = await this.#resolveSession(workerId);
        if (definition !== undefined && (await this.#canReadSession(principal, definition))) {
          allowed.push(workerId);
        } else {
          refused.add(workerId);
        }
      } catch {
        refused.add(workerId);
      }
    }
    return { memberWorkerIds: allowed, refusedWorkerIds: [...refused] };
  }

  /** Ask the policy gate, defaulting to allow when none is configured. */
  async #decide(data: string, groupId: string, principal: string): Promise<FanOutPolicyDecision> {
    if (this.#policyGate === undefined) {
      return { action: "allow" };
    }
    return this.#policyGate.interceptFanout(
      data,
      {
        worker_id: `group:${groupId}`,
        client_id: principal,
        role: "admin",
        action: "fanout_send",
        metadata: { is_fanout: true, group_id: groupId },
      },
      groupId,
    );
  }

  /** Park a command for approval and audit that it was held. */
  async #hold(group: FanOutGroup, data: string, principal: string, options: SendOptions): Promise<FanOutResult> {
    const requestId = this.#newId();
    this.#pending.set(requestId, {
      groupId: group.groupId,
      command: data,
      principal,
      quiesceMs: options.quiesceMs,
      maxResponseMs: options.maxResponseMs,
    });

    const now = this.#now();
    this.#hub.addApproval({
      id: requestId,
      worker_id: `group:${group.groupId}`,
      submitter_id: principal,
      command: data,
      status: "pending",
      created_at: now,
      expires_at: now + APPROVAL_WINDOW_S,
      group_id: group.groupId,
      is_fanout: true,
    });

    // A held command is a security event, logged whether or not anyone ever
    // approves it. The command is truncated because the audit log is not a
    // transcript store and a caller should not choose how much it writes.
    await this.#hub.appendEvent(`group:${group.groupId}`, "terminal.fanout.hold", {
      group_id: group.groupId,
      command: data.slice(0, AUDIT_COMMAND_CHARS),
      request_id: requestId,
      principal,
    });

    return {
      ...this.#emptyResult(group.groupId, data, requestId),
      approvalRequired: true,
      approvalId: requestId,
    };
  }

  /** Run the send in whichever mode the group asks for. */
  async #dispatch(group: FanOutGroup, data: string, principal: string, options: SendOptions): Promise<FanOutResult> {
    const timings = {
      quiesceMs: options.quiesceMs ?? group.quiesceMs,
      maxMs: options.maxResponseMs ?? group.maxResponseMs,
    };
    // Compared against the literal, matching the reference: an unrecognised
    // mode fans out rather than failing closed.
    const dispatchGroup = { ...group, workerIds: options.memberWorkerIds ?? [...group.workerIds] };
    const result =
      group.mode === "sequential"
        ? await this.#sendSequential(dispatchGroup, data, principal, timings)
        : await this.#sendParallel(dispatchGroup, data, principal, timings);
    for (const workerId of options.refusedWorkerIds ?? []) {
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
    await this.#announce(group, data, sendId, principal);

    const frame = { type: "input", data, ts: sentAt };
    const accepted = await Promise.all(
      group.workerIds.map(async (workerId) => {
        try {
          return await this.#hub.sendWorker(workerId, frame);
        } catch {
          return false;
        }
      }),
    );

    // Only sessions that took the command can answer; waiting on the rest is
    // latency the whole fan-out pays for nothing.
    const collected = await Promise.all(
      group.workerIds.map(async (workerId, index) => {
        if (accepted[index] !== true) {
          return undefined;
        }
        try {
          return await this.#hub.collectOutput(workerId, timings);
        } catch {
          return undefined;
        }
      }),
    );

    const answered: Answered[] = [];
    const results = group.workerIds.map((workerId, index) => {
      const output = collected[index];
      if (output === undefined) {
        return this.#failedRow(workerId);
      }
      answered.push({ index, output: output.output });
      return { workerId, ok: true, outputDelta: output.output, elapsedMs: output.elapsedMs, divergent: false };
    });
    return this.#finish(group, data, sendId, sentAt, results, answered);
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
    await this.#announce(group, data, sendId, principal);

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
      if (!(await this.#hub.sendWorker(workerId, frame))) {
        results.push(this.#failedRow(workerId));
        continue;
      }
      const { output, elapsedMs } = await this.#hub.collectOutput(workerId, timings);
      answered.push({ index: results.length, output });
      results.push({ workerId, ok: true, outputDelta: output, elapsedMs, divergent: false });

      if (group.stopOnFirstError && group.errorPattern !== undefined) {
        stopped = new RegExp(group.errorPattern).test(output);
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
