//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The in-memory control-plane backend.
 *
 * Port of the Python package `provide.uterm.control.plane.memory`.
 *
 * Not a toy: it has to behave like the SQLite backend, or a deployment that
 * develops against memory finds out the difference in production. Two
 * overlapping transactions that write the same key cannot both succeed —
 * detected optimistically at commit, which is what SQLite gets by serialising
 * — and a transaction applies only the keys *it* changed, so two that touch
 * different rows both commit rather than the later silently undoing the first.
 */

import { ControlPlaneConflictError } from "./errors.ts";
import {
  type ApprovalRecord,
  CONTROL_PLANE_DEFAULTS,
  type ControlPlaneConfig,
  DEFAULT_CAPABILITIES,
  type EngineCapabilities,
  type GraphicalTargetRecord,
  type LeaseRecord,
  type ResumeTokenRecord,
  type SessionRecord,
  type SessionTokenRecord,
} from "./types.ts";

/** Everything the backend holds. */
export interface MemoryState {
  sessionTokens: Map<string, SessionTokenRecord>;
  resumeTokens: Map<string, ResumeTokenRecord>;
  sessions: Map<string, SessionRecord>;
  approvals: Map<string, ApprovalRecord>;
  leases: Map<string, LeaseRecord>;
  graphicalTargets: Map<string, GraphicalTargetRecord>;
  /**
   * The audit chain's head, once there is one.
   *
   * Deliberately *not* copied into a transaction: it is set outside one, and
   * it is non-durable — the memory backend loses it on restart, so
   * cross-restart anti-rollback only holds on SQLite.
   */
  auditHead?: [number, string] | undefined;
}

/** The key a session token is filed under. */
function tokenKey(sessionId: string, tokenKind: string): string {
  return JSON.stringify([sessionId, tokenKind]);
}

/** A fresh, empty state. */
function emptyState(): MemoryState {
  return {
    sessionTokens: new Map(),
    resumeTokens: new Map(),
    sessions: new Map(),
    approvals: new Map(),
    leases: new Map(),
    graphicalTargets: new Map(),
  };
}

/** A shallow copy: the records themselves are never mutated in place. */
function copyState(state: MemoryState): MemoryState {
  return {
    sessionTokens: new Map(state.sessionTokens),
    resumeTokens: new Map(state.resumeTokens),
    sessions: new Map(state.sessions),
    approvals: new Map(state.approvals),
    leases: new Map(state.leases),
    graphicalTargets: new Map(state.graphicalTargets),
  };
}

/** The tables a transaction copies and merges. */
const TABLES = ["sessionTokens", "resumeTokens", "sessions", "approvals", "leases", "graphicalTargets"] as const;

/**
 * Whether a key this transaction wrote was changed under it.
 *
 * For every key whose value differs between the snapshot and the working
 * copy — that is, every key this transaction wrote or deleted — the shared
 * table must still hold what the snapshot saw.
 */
function detectConflict<V>(root: Map<string, V>, snapshot: Map<string, V>, working: Map<string, V>): boolean {
  for (const key of new Set([...snapshot.keys(), ...working.keys()])) {
    const before = snapshot.get(key);
    const after = working.get(key);
    if (after === before) {
      continue;
    }
    if (root.get(key) !== before) {
      return true;
    }
  }
  return false;
}

/** Apply only this transaction's key-level changes. */
function mergeTable<V>(root: Map<string, V>, snapshot: Map<string, V>, working: Map<string, V>): void {
  for (const key of new Set([...snapshot.keys(), ...working.keys()])) {
    const before = snapshot.get(key);
    const after = working.get(key);
    if (after === before) {
      continue;
    }
    if (working.has(key)) {
      root.set(key, working.get(key) as V);
    } else {
      root.delete(key);
    }
  }
}

/** One unit of work against the backend. */
export class MemoryTransaction {
  /** The transaction's own view, which its stores read and write. */
  readonly state: MemoryState;
  /** Whether it has been committed or rolled back. */
  closed = false;
  readonly #root: MemoryState;
  readonly #snapshot: MemoryState;

  constructor(root: MemoryState) {
    this.#root = root;
    this.#snapshot = copyState(root);
    this.state = copyState(root);
  }

  /**
   * Apply this transaction's writes.
   *
   * @throws {ControlPlaneConflictError} When a key it wrote was changed by a
   *   transaction that committed first. Detected across every table *before*
   *   anything is merged, so a conflict leaves no partial write behind.
   */
  async commit(): Promise<void> {
    // A second commit is a no-op rather than a failure: a caller that
    // commits in a finally block should not have to track whether it already
    // did.
    if (this.closed) {
      return;
    }
    const conflict = TABLES.some((table) =>
      detectConflict(
        this.#root[table] as Map<string, unknown>,
        this.#snapshot[table] as Map<string, unknown>,
        this.state[table] as Map<string, unknown>,
      ),
    );
    if (conflict) {
      this.closed = true;
      throw new ControlPlaneConflictError("memory control-plane transaction conflicts with a concurrent commit");
    }
    for (const table of TABLES) {
      mergeTable(
        this.#root[table] as Map<string, unknown>,
        this.#snapshot[table] as Map<string, unknown>,
        this.state[table] as Map<string, unknown>,
      );
    }
    this.closed = true;
  }

  /** Discard this transaction's writes. */
  async rollback(): Promise<void> {
    this.closed = true;
  }
}

/** Sessions, as this transaction sees them. */
export class MemorySessionStore {
  readonly #state: MemoryState;

  constructor(state: MemoryState) {
    this.#state = state;
  }

  /** Store a session, replacing any earlier version. */
  async upsertSession(record: SessionRecord): Promise<void> {
    this.#state.sessions.set(record.sessionId, record);
  }

  /** One session, if it is there. */
  async getSession(sessionId: string): Promise<SessionRecord | undefined> {
    return this.#state.sessions.get(sessionId);
  }

  /**
   * Mark a session deleted.
   *
   * The row stays: a session that vanished would take its audit trail with
   * it, and the reaper is what eventually removes it.
   */
  async markDeleted(sessionId: string, deletedAt: number): Promise<void> {
    const current = this.#state.sessions.get(sessionId);
    if (current === undefined) {
      return;
    }
    this.#state.sessions.set(sessionId, { ...current, deletedAt, lifecycleState: "deleted" });
  }
}

/** Tokens, as this transaction sees them. */
export class MemoryTokenStore {
  readonly #state: MemoryState;

  constructor(state: MemoryState) {
    this.#state = state;
  }

  /** Store a session token, keyed by session *and* purpose. */
  async putSessionToken(record: SessionTokenRecord): Promise<void> {
    this.#state.sessionTokens.set(tokenKey(record.sessionId, record.tokenKind), record);
  }

  /** One session token, if it is there. */
  async getSessionToken(sessionId: string, tokenKind: string): Promise<SessionTokenRecord | undefined> {
    return this.#state.sessionTokens.get(tokenKey(sessionId, tokenKind));
  }

  /** Store a resume token. */
  async createResumeToken(record: ResumeTokenRecord): Promise<void> {
    this.#state.resumeTokens.set(record.tokenValue, record);
  }

  /**
   * A resume token, if it is usable.
   *
   * A revoked one reads as absent rather than as revoked: every caller of
   * this is asking "may this token resume", and a revoked token may not.
   */
  async getResumeToken(tokenValue: string): Promise<ResumeTokenRecord | undefined> {
    const record = this.#state.resumeTokens.get(tokenValue);
    if (record === undefined || record.revokedAt !== undefined) {
      return undefined;
    }
    return record;
  }

  /** Withdraw a resume token, keeping the row for the reaper. */
  async revokeResumeToken(tokenValue: string, revokedAt: number): Promise<void> {
    const record = this.#state.resumeTokens.get(tokenValue);
    if (record === undefined) {
      return;
    }
    this.#state.resumeTokens.set(tokenValue, { ...record, revokedAt });
  }
}

/** Approvals, as this transaction sees them. */
export class MemoryApprovalStore {
  readonly #state: MemoryState;

  constructor(state: MemoryState) {
    this.#state = state;
  }

  /** Store an approval, replacing any earlier version. */
  async putApproval(record: ApprovalRecord): Promise<void> {
    this.#state.approvals.set(record.approvalId, record);
  }

  /** One approval, if it is there. */
  async getApproval(approvalId: string): Promise<ApprovalRecord | undefined> {
    return this.#state.approvals.get(approvalId);
  }

  /**
   * Everything still waiting, oldest first.
   *
   * Sorted by creation and then by id, matching the SQLite backend's
   * `ORDER BY created_at ASC, approval_id ASC`, so a queue consumer sees the
   * same order whichever backend it is on.
   */
  async listPending(): Promise<ApprovalRecord[]> {
    return [...this.#state.approvals.values()]
      .filter((record) => record.state === "pending")
      .sort((left, right) =>
        left.createdAt === right.createdAt
          ? left.approvalId.localeCompare(right.approvalId)
          : left.createdAt - right.createdAt,
      );
  }
}

/** Leases, as this transaction sees them. */
export class MemoryLeaseStore {
  readonly #state: MemoryState;

  constructor(state: MemoryState) {
    this.#state = state;
  }

  /** Store a lease, replacing any earlier one for that session. */
  async putLease(record: LeaseRecord): Promise<void> {
    this.#state.leases.set(record.sessionId, record);
  }

  /** The lease on a session, if there is one. */
  async getLease(sessionId: string): Promise<LeaseRecord | undefined> {
    return this.#state.leases.get(sessionId);
  }

  /** Drop the lease on a session. Nothing to do if there was none. */
  async clearLease(sessionId: string): Promise<void> {
    this.#state.leases.delete(sessionId);
  }
}

/** Graphical targets, as this transaction sees them. */
export class MemoryGraphicalTargetStore {
  readonly #state: MemoryState;

  constructor(state: MemoryState) {
    this.#state = state;
  }

  /** Store a target. */
  async putTarget(record: GraphicalTargetRecord): Promise<void> {
    this.#state.graphicalTargets.set(record.targetId, record);
  }

  /** One target, if it is there. */
  async getTarget(targetId: string): Promise<GraphicalTargetRecord | undefined> {
    return this.#state.graphicalTargets.get(targetId);
  }

  /** Drop a target. */
  async deleteTarget(targetId: string): Promise<void> {
    this.#state.graphicalTargets.delete(targetId);
  }
}

/** What every backend offers, whichever one a deployment is on. */
export interface ControlPlane {
  /** What this engine can do. */
  readonly capabilities: EngineCapabilities;
  /** Prepare the backend. */
  open(): Promise<void>;
  /** Release it. */
  close(): Promise<void>;
  /** Bring its schema up to date. */
  migrate(): Promise<void>;
  /** Start a unit of work. */
  begin(): Promise<MemoryTransaction>;
  /** Drop rows older than the cutoff, returning how many went. */
  reap(options: { now: number; retentionS: number }): Promise<number>;
  /** The audit chain's head, if one has been recorded. */
  getAuditHead(): Promise<[number, string] | undefined>;
  /** Move the head forward. A sequence that is not higher is a no-op. */
  setAuditHead(seq: number, recordHash: string): Promise<void>;
}

/** An in-memory control plane with shared, mutable state. */
export class MemoryControlPlane implements ControlPlane {
  /** What this engine can do. */
  readonly capabilities: EngineCapabilities;
  /** How it was configured. */
  readonly config: Required<Pick<ControlPlaneConfig, "backend" | "databaseUrl">>;
  readonly #state: MemoryState = emptyState();

  constructor(config: ControlPlaneConfig = {}) {
    this.capabilities = config.capabilities ?? DEFAULT_CAPABILITIES;
    this.config = {
      backend: config.backend ?? CONTROL_PLANE_DEFAULTS.backend,
      databaseUrl: config.databaseUrl ?? CONTROL_PLANE_DEFAULTS.databaseUrl,
    };
  }

  /** Nothing to open. */
  async open(): Promise<void> {}

  /** Nothing to close. */
  async close(): Promise<void> {}

  /** Nothing to migrate: there is no schema. */
  async migrate(): Promise<void> {}

  /** Start a unit of work. */
  async begin(): Promise<MemoryTransaction> {
    return new MemoryTransaction(this.#state);
  }

  /**
   * Drop rows whose soft-delete or expiry is older than the cutoff.
   *
   * The predicate mirrors SQLite's — strict `<`, and an absent timestamp
   * never matches — so both backends prune exactly the same rows.
   *
   * @returns How many records went.
   */
  async reap(options: { now: number; retentionS: number }): Promise<number> {
    const cutoff = options.now - options.retentionS;
    const state = this.#state;
    const before =
      state.resumeTokens.size +
      state.sessionTokens.size +
      state.sessions.size +
      state.leases.size +
      state.approvals.size;

    keepWhere(state.resumeTokens, (record) => !(isOlder(record.revokedAt, cutoff) || record.expiresAt < cutoff));
    keepWhere(
      state.sessionTokens,
      (record) => !(isOlder(record.revokedAt, cutoff) || isOlder(record.expiresAt, cutoff)),
    );
    keepWhere(state.sessions, (record) => !isOlder(record.deletedAt, cutoff));
    keepWhere(state.leases, (record) => !(isOlder(record.deletedAt, cutoff) || record.leaseExpiresAt < cutoff));
    keepWhere(state.approvals, (record) => !isOlder(record.resolvedAt, cutoff));

    const after =
      state.resumeTokens.size +
      state.sessionTokens.size +
      state.sessions.size +
      state.leases.size +
      state.approvals.size;
    return before - after;
  }

  /**
   * The audit chain's head, if one has been recorded.
   *
   * Non-durable: this backend loses it on restart, which is consistent with
   * its documented volatility but means cross-restart anti-rollback only
   * holds on SQLite.
   */
  async getAuditHead(): Promise<[number, string] | undefined> {
    return this.#state.auditHead;
  }

  /**
   * Move the audit head forward.
   *
   * A sequence that is not higher is a no-op — that is the anti-rollback
   * guard, and without it a replayed older head would be accepted as an
   * update.
   */
  async setAuditHead(seq: number, recordHash: string): Promise<void> {
    const current = this.#state.auditHead;
    if (current !== undefined && current[0] >= seq) {
      return;
    }
    this.#state.auditHead = [seq, recordHash];
  }

  /** Sessions, as `tx` sees them. */
  sessionStore(tx: MemoryTransaction): MemorySessionStore {
    return new MemorySessionStore(tx.state);
  }

  /** Tokens, as `tx` sees them. */
  tokenStore(tx: MemoryTransaction): MemoryTokenStore {
    return new MemoryTokenStore(tx.state);
  }

  /** Approvals, as `tx` sees them. */
  approvalStore(tx: MemoryTransaction): MemoryApprovalStore {
    return new MemoryApprovalStore(tx.state);
  }

  /** Leases, as `tx` sees them. */
  leaseStore(tx: MemoryTransaction): MemoryLeaseStore {
    return new MemoryLeaseStore(tx.state);
  }

  /** Graphical targets, as `tx` sees them. */
  graphicalTargetStore(tx: MemoryTransaction): MemoryGraphicalTargetStore {
    return new MemoryGraphicalTargetStore(tx.state);
  }
}

/** Whether a timestamp is present and strictly older than the cutoff. */
function isOlder(timestamp: number | undefined, cutoff: number): boolean {
  return timestamp !== undefined && timestamp < cutoff;
}

/** Drop every entry the predicate rejects. */
function keepWhere<V>(table: Map<string, V>, keep: (record: V) => boolean): void {
  for (const [key, record] of [...table]) {
    if (!keep(record)) {
      table.delete(key);
    }
  }
}

/**
 * Build the backend a configuration asks for.
 *
 * An unknown backend is a refusal rather than a fallback: a deployment that
 * asked for a durable store must not silently get a volatile one.
 *
 * @throws {Error} On a backend nothing implements.
 */
export async function bootstrapControlPlane(config: ControlPlaneConfig = {}): Promise<ControlPlane> {
  const backend = config.backend ?? CONTROL_PLANE_DEFAULTS.backend;
  if (backend === "memory") {
    return new MemoryControlPlane(config);
  }
  // The SQLite backend is not ported yet; naming it here rather than falling
  // through keeps the refusal message the reference's.
  throw new Error(`unsupported control-plane backend: ${backend}`);
}
