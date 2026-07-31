//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Fan-out group records and their storage.
 *
 * Port of the Python modules `provide.uterm.server.bridge.fanout._models`
 * and `..._store`.
 */

/**
 * How a group's sessions are driven.
 *
 * Left open rather than a closed union: the reference accepts any string and
 * the controller is what gives a mode meaning, so narrowing it here would
 * reject a configuration the Python server accepts.
 */
export type FanOutMode = string;

/** A named group of worker sessions that take broadcast input together. */
export interface FanOutGroup {
  /** Identifier the API refers to. */
  groupId: string;
  /** Human-facing label. */
  name: string;
  /** The sessions that receive the broadcast. */
  workerIds: string[];
  /** Principal that created the group, and always sees it. */
  createdBy: string;
  /** Wall-clock seconds at creation. */
  createdAt: number;
  /** How the sessions are driven. */
  mode: FanOutMode;
  /** Whether the first failure abandons the rest of the group. */
  stopOnFirstError: boolean;
  /** Pattern that marks a session's output as a failure. */
  errorPattern?: string | undefined;
  /** How long a session must be silent before its output is considered done. */
  quiesceMs: number;
  /** Hard cap on how long to wait for any one session. */
  maxResponseMs: number;
  /** Similarity below which a session counts as divergent. */
  divergenceThreshold: number;
  /** Principals granted access besides the creator. */
  grants: string[];
}

/** Options for {@link fanOutGroup}. */
export interface FanOutGroupOptions {
  groupId: string;
  name: string;
  workerIds: string[];
  createdBy: string;
  createdAt: number;
  mode?: FanOutMode;
  stopOnFirstError?: boolean;
  errorPattern?: string;
  quiesceMs?: number;
  maxResponseMs?: number;
  divergenceThreshold?: number;
  grants?: string[];
}

/** What one session did with a fan-out send. */
export interface SessionFanOutResult {
  /** The session this describes. */
  workerId: string;
  /** Whether the send reached it and produced output. */
  ok: boolean;
  /** What it printed, if anything. */
  outputDelta?: string | undefined;
  /** How long it took to go quiet. */
  elapsedMs: number;
  /** Whether its output disagreed with the group's consensus. */
  divergent: boolean;
}

/** The aggregate of a fan-out send. */
export interface FanOutResult {
  /** The group the command went to. */
  groupId: string;
  /** Identifier for this send, for correlating later polls. */
  sendId: string;
  /** What was sent. */
  command: string;
  /** Wall-clock seconds at dispatch. */
  sentAt: number;
  /** One entry per session. */
  results: SessionFanOutResult[];
  /** Sessions whose output disagreed with the consensus. */
  divergentSessions: string[];
  /** Sessions the send did not reach or that reported failure. */
  failedSessions: string[];
  /** Why the send as a whole failed, when it did. */
  error: string | null;
  /** Whether the command is held pending approval. */
  approvalRequired: boolean;
  /** The approval to resolve, when one is required. */
  approvalId: string | null;
}

/** Options for {@link fanOutResult}. */
export interface FanOutResultOptions {
  groupId: string;
  sendId: string;
  command: string;
  sentAt: number;
  results: SessionFanOutResult[];
  divergentSessions: string[];
  failedSessions: string[];
  error?: string;
  approvalRequired?: boolean;
  approvalId?: string;
}

/**
 * Build a group, applying the reference defaults.
 *
 * The defaults are policy rather than convenience: they decide how long the
 * hub waits for a session to go quiet, how far outputs may drift before
 * counting as divergent, and whether one failure abandons the rest.
 */
export function fanOutGroup(options: FanOutGroupOptions): FanOutGroup {
  return {
    groupId: options.groupId,
    name: options.name,
    workerIds: options.workerIds,
    createdBy: options.createdBy,
    createdAt: options.createdAt,
    mode: options.mode ?? "parallel",
    stopOnFirstError: options.stopOnFirstError ?? false,
    errorPattern: options.errorPattern,
    quiesceMs: options.quiesceMs ?? 500,
    maxResponseMs: options.maxResponseMs ?? 10_000,
    divergenceThreshold: options.divergenceThreshold ?? 0.8,
    // A fresh array per group: sharing one would make a grant on any group a
    // grant on every group built from the defaults.
    grants: [...(options.grants ?? [])],
  };
}

/** Build a result, applying the reference defaults. */
export function fanOutResult(options: FanOutResultOptions): FanOutResult {
  return {
    groupId: options.groupId,
    sendId: options.sendId,
    command: options.command,
    sentAt: options.sentAt,
    results: options.results,
    divergentSessions: options.divergentSessions,
    failedSessions: options.failedSessions,
    error: options.error ?? null,
    approvalRequired: options.approvalRequired ?? false,
    approvalId: options.approvalId ?? null,
  };
}

/** Persistence for fan-out groups. */
export interface FanOutStore {
  /** Insert or replace a group. */
  save(group: FanOutGroup): Promise<void>;
  /** The group with this id, if it exists. */
  get(groupId: string): Promise<FanOutGroup | undefined>;
  /** Remove a group; absent ids are ignored. */
  delete(groupId: string): Promise<void>;
  /** Every group this principal may see. */
  listForPrincipal(principal: string): Promise<FanOutGroup[]>;
  /** Atomically grant access when `creator` still owns the group. */
  grantAccess(groupId: string, grantee: string, creator: string): Promise<boolean>;
}

/** Copy every mutable field across the persistence trust boundary. */
function cloneGroup(group: FanOutGroup): FanOutGroup {
  return {
    ...group,
    workerIds: [...group.workerIds],
    grants: [...group.grants],
  };
}

/** Ephemeral store. Groups are lost on restart. */
export class InMemoryFanOutStore implements FanOutStore {
  readonly #groups = new Map<string, FanOutGroup>();

  /** Insert or replace the group with this id. */
  async save(group: FanOutGroup): Promise<void> {
    this.#groups.set(group.groupId, cloneGroup(group));
  }

  /** The group with this id, if it exists. */
  async get(groupId: string): Promise<FanOutGroup | undefined> {
    const group = this.#groups.get(groupId);
    return group === undefined ? undefined : cloneGroup(group);
  }

  /** Remove a group. An id that is not held is ignored rather than an error. */
  async delete(groupId: string): Promise<void> {
    this.#groups.delete(groupId);
  }

  /**
   * Every group `principal` may see: the ones it created, plus the ones it
   * has been granted.
   *
   * The rule is the access boundary. Too narrow and an operator cannot see
   * their own groups; too wide and they can drive someone else's fleet.
   *
   * The list is a snapshot, so a caller iterating it while groups are saved
   * does not observe the mutation.
   */
  async listForPrincipal(principal: string): Promise<FanOutGroup[]> {
    return [...this.#groups.values()]
      .filter((group) => group.createdBy === principal || group.grants.includes(principal))
      .map(cloneGroup);
  }

  /** Add a grant without a detached read/modify/write race. */
  async grantAccess(groupId: string, grantee: string, creator: string): Promise<boolean> {
    const group = this.#groups.get(groupId);
    if (group === undefined || group.createdBy !== creator) return false;
    if (!group.grants.includes(grantee)) group.grants.push(grantee);
    return true;
  }
}
