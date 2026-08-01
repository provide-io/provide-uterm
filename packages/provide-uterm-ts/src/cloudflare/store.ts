//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The Durable Object's own SQLite, where a session's state survives eviction.
 *
 * Port of the Python module `provide.uterm.cloudflare.state.store`.
 *
 * Everything a reconnecting browser is told comes back through here, so the
 * shapes have to match the reference's exactly — a Durable Object and the
 * Python Worker may run against the same storage.
 *
 * The reference carries a set of row-shape adapters, because Pyodide hands
 * back rows as dicts, JS proxies, cursors or attribute-only objects depending
 * on the runtime. Neither the Workers binding nor `node:sqlite` does that:
 * a row is a plain object, so there is nothing to adapt and no shim here.
 */

/** Runs one statement and hands back whatever rows it produced. */
export type SqlExecutor = (sql: string, ...params: unknown[]) => Iterable<Record<string, unknown>> | undefined;

/** Who holds the right to type, and until when. */
export interface LeaseRecord {
  workerId: string;
  hijackId: string;
  owner: string;
  leaseExpiresAt: number;
  /** The connection that acquired the lease, when the caller says. */
  acquiredBy?: string | null;
}

/** What a session says about itself. */
export interface SessionMetaRecord {
  display_name: string;
  connector_type: string;
  created_at: number;
  tags: unknown[];
  visibility: string;
  owner: unknown;
}

/** A session's persisted state. */
export interface SessionStateRecord {
  worker_id: unknown;
  hijack_id: unknown;
  owner: unknown;
  lease_expires_at: unknown;
  last_snapshot: unknown;
  event_seq: number;
  input_mode: string;
  deleted_at: unknown;
  acquired_by: unknown;
  worker_generation: unknown;
}

/** One thing that happened to a session. */
export interface SessionEvent {
  seq: number;
  ts: number;
  type: string;
  data: unknown;
}

/** A registered webhook. */
export interface WebhookRecord {
  webhook_id: string;
  session_id: string;
  url: string;
  event_types: unknown;
  pattern: unknown;
  secret: unknown;
}

/** A token that lets a browser pick a session back up. */
export interface ResumeTokenRecord {
  token: unknown;
  worker_id: unknown;
  role: string;
  was_hijack_owner: boolean;
  created_at: number;
  expires_at: number;
}

/** One entry in the recording view of the log. */
export interface RecordingEntry {
  ts: number;
  event: string;
  data: unknown;
}

/** How a recording is queried. */
export interface RecordingQuery {
  limit?: number;
  /** Where to start. Absent means the tail. */
  offset?: number;
  /** Only entries of this type. */
  event?: string;
}

/** Options for {@link SqliteStateStore}. */
export interface SqliteStateStoreOptions {
  maxEventsPerWorker?: number;
  /** Wall clock in seconds. */
  now?: () => number;
}

/** The tables a session needs. */
const SCHEMA: readonly string[] = [
  `CREATE TABLE IF NOT EXISTS session_state (
                worker_id TEXT PRIMARY KEY, hijack_id TEXT, owner TEXT,
                lease_expires_at REAL, last_snapshot_json TEXT, deleted_at REAL,
                event_seq INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
                acquired_by TEXT, worker_generation TEXT)`,
  `CREATE TABLE IF NOT EXISTS session_events (
                worker_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
                event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (worker_id, seq))`,
  `CREATE TABLE IF NOT EXISTS runtime_activations (
                worker_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL,
                activation_seq INTEGER NOT NULL, activated_at REAL NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS resume_tokens (
                token TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer', was_hijack_owner INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, expires_at REAL NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS webhooks (
                webhook_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, url TEXT NOT NULL,
                event_types_json TEXT, pattern TEXT, secret TEXT)`,
  `CREATE TABLE IF NOT EXISTS session_meta (
                worker_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                connector_type TEXT NOT NULL DEFAULT 'unknown', created_at REAL NOT NULL DEFAULT 0,
                tags_json TEXT NOT NULL DEFAULT '[]', visibility TEXT NOT NULL DEFAULT 'public',
                owner TEXT)`,
  `CREATE TABLE IF NOT EXISTS tunnel_invite_state (
                worker_id TEXT PRIMARY KEY, entry_json TEXT NOT NULL, updated_at REAL NOT NULL)`,
];

/**
 * Columns added after the first schema shipped.
 *
 * Applied inside a suppressed failure, because every start after the first
 * finds them already there — and a store that refused to open would lose the
 * session rather than the column.
 */
const LATER_COLUMNS: readonly string[] = [
  "ALTER TABLE session_state ADD COLUMN input_mode TEXT NOT NULL DEFAULT 'hijack'",
  "ALTER TABLE session_state ADD COLUMN deleted_at REAL",
  "ALTER TABLE session_state ADD COLUMN acquired_by TEXT",
  "ALTER TABLE session_state ADD COLUMN worker_generation TEXT",
];

/** A value the caller actually supplied. */
function supplied<T>(value: T | undefined | null | ""): T | undefined {
  return value === undefined || value === null || value === "" ? undefined : (value as T);
}

/** Session state, persisted in the Durable Object's SQLite. */
export class SqliteStateStore {
  readonly #exec: SqlExecutor;
  readonly #now: () => number;
  readonly #maxEvents: number;

  constructor(exec: SqlExecutor, options: SqliteStateStoreOptions = {}) {
    this.#exec = exec;
    this.#now = options.now ?? (() => Date.now() / 1000);
    // At least one: a store that could hold no events would drop each as it
    // arrived and leave a reconnecting browser nothing to catch up on.
    this.#maxEvents = Math.max(1, options.maxEventsPerWorker ?? 2000);
  }

  /** How many events one session may keep. */
  get maxEvents(): number {
    return this.#maxEvents;
  }

  /** Run a statement and hand back its rows. */
  #rows(sql: string, ...params: unknown[]): Array<Record<string, unknown>> {
    return [...(this.#exec(sql, ...params) ?? [])];
  }

  /** The first row a statement produced, if any. */
  #row(sql: string, ...params: unknown[]): Record<string, unknown> | undefined {
    return this.#rows(sql, ...params)[0];
  }

  /** Create the tables, and add the later columns if they are missing. */
  migrate(): void {
    for (const ddl of SCHEMA) {
      this.#rows(ddl);
    }
    for (const ddl of LATER_COLUMNS) {
      try {
        this.#rows(ddl);
      } catch {
        // Already there, which is the case on every start after the first.
      }
    }
  }

  /** Write what a session says about itself, replacing what was there. */
  saveSessionMeta(workerId: string, meta: Partial<SessionMetaRecord> & { tags?: unknown[] }): void {
    this.#rows(
      "INSERT INTO session_meta(worker_id,display_name,connector_type,created_at,tags_json,visibility,owner) " +
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET display_name=excluded.display_name," +
        "connector_type=excluded.connector_type,created_at=excluded.created_at," +
        "tags_json=excluded.tags_json,visibility=excluded.visibility,owner=excluded.owner",
      workerId,
      String(supplied(meta.display_name) ?? workerId),
      String(supplied(meta.connector_type) ?? "unknown"),
      // Zero is not a time a session was created at, so it is stamped too.
      Number(supplied(meta.created_at) === undefined || meta.created_at === 0 ? this.#now() : meta.created_at),
      JSON.stringify(supplied(meta.tags) ?? []),
      String(supplied(meta.visibility) ?? "public"),
      (meta.owner ?? null) as string | null,
    );
  }

  /** Read what a session said about itself. */
  loadSessionMeta(workerId: string): SessionMetaRecord | undefined {
    const row = this.#row(
      "SELECT display_name,connector_type,created_at,tags_json,visibility,owner FROM session_meta WHERE worker_id=?",
      workerId,
    );
    if (row === undefined) {
      return undefined;
    }
    return {
      display_name: String(supplied(row.display_name) ?? workerId),
      connector_type: String(supplied(row.connector_type) ?? "unknown"),
      // NOT NULL with a zero default, so there is nothing to fall back to.
      created_at: Number(row.created_at),
      tags: JSON.parse(String(supplied(row.tags_json) ?? "[]")) as unknown[],
      visibility: String(supplied(row.visibility) ?? "public"),
      owner: row.owner ?? null,
    };
  }

  /**
   * Read which one-time invites have already been redeemed.
   *
   * An entry that cannot be read counts as nothing redeemed. That is the safe
   * direction: an invite may be offered again rather than a session being
   * locked out of its own tunnel.
   */
  loadTunnelInviteState(workerId: string): Record<string, unknown> | undefined {
    const row = this.#row("SELECT entry_json FROM tunnel_invite_state WHERE worker_id=?", workerId);
    if (row === undefined) {
      return undefined;
    }
    try {
      const value: unknown = JSON.parse(String(row.entry_json));
      return typeof value === "object" && value !== null && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : undefined;
    } catch {
      return undefined;
    }
  }

  /** Record which one-time invites have been redeemed. */
  saveTunnelInviteState(workerId: string, entry: Record<string, unknown>): void {
    this.#rows(
      "INSERT INTO tunnel_invite_state(worker_id,entry_json,updated_at) VALUES(?,?,?) " +
        "ON CONFLICT(worker_id) DO UPDATE SET entry_json=excluded.entry_json,updated_at=excluded.updated_at",
      workerId,
      JSON.stringify(entry),
      this.#now(),
    );
  }

  /** Read a session's persisted state. */
  loadSession(workerId: string): SessionStateRecord | undefined {
    const row = this.#row(
      `
                SELECT worker_id, hijack_id, owner, lease_expires_at, last_snapshot_json, event_seq, input_mode
                     , deleted_at, acquired_by, worker_generation
                FROM session_state
                WHERE worker_id = ?
                `,
      workerId,
    );
    if (row === undefined) {
      return undefined;
    }
    const snapshot = row.last_snapshot_json;
    return {
      worker_id: row.worker_id,
      hijack_id: row.hijack_id ?? null,
      owner: row.owner ?? null,
      lease_expires_at: row.lease_expires_at ?? null,
      last_snapshot: supplied(snapshot) === undefined ? null : (JSON.parse(String(snapshot)) as unknown),
      // As above: NOT NULL with a zero default.
      event_seq: Number(row.event_seq),
      input_mode: String(supplied(row.input_mode) ?? "hijack"),
      deleted_at: row.deleted_at ?? null,
      acquired_by: row.acquired_by ?? null,
      worker_generation: row.worker_generation ?? null,
    };
  }

  /** Record who holds the right to type, and until when. */
  saveLease(record: LeaseRecord): void {
    this.#rows(
      `
            INSERT INTO session_state(worker_id, hijack_id, owner, lease_expires_at, acquired_by, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                hijack_id = excluded.hijack_id,
                owner = excluded.owner,
                lease_expires_at = excluded.lease_expires_at,
                acquired_by = excluded.acquired_by,
                updated_at = excluded.updated_at
            `,
      record.workerId,
      record.hijackId,
      record.owner,
      Number(record.leaseExpiresAt),
      record.acquiredBy ?? null,
      this.#now(),
    );
  }

  /**
   * Give up the lease.
   *
   * All three fields together: a half-cleared lease leaves an owner with no
   * expiry, which reads as one that never ends. The snapshot and the mode
   * stay — giving up the keyboard is not losing the screen.
   */
  clearLease(workerId: string): void {
    this.#rows(
      `
            UPDATE session_state
            SET hijack_id = NULL, owner = NULL, lease_expires_at = NULL, acquired_by = NULL, updated_at = ?
            WHERE worker_id = ?
            `,
      this.#now(),
      workerId,
    );
  }

  /**
   * Leave a tombstone for a deleted session.
   *
   * The row stays, so a request arriving afterwards is told the session is
   * gone rather than that it never existed. Nothing else of it remains
   * readable.
   */
  markDeleted(workerId: string): void {
    const now = this.#now();
    this.#rows(
      `
            INSERT INTO session_state(worker_id, deleted_at, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                hijack_id = NULL,
                owner = NULL,
                lease_expires_at = NULL,
                acquired_by = NULL,
                last_snapshot_json = NULL,
                worker_generation = NULL,
                deleted_at = excluded.deleted_at,
                updated_at = excluded.updated_at
            `,
      workerId,
      now,
      now,
    );
  }

  /** Record how input reaches the session. */
  saveInputMode(workerId: string, mode: string): void {
    const now = this.#now();
    this.#rows(
      `
            INSERT INTO session_state(worker_id, input_mode, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                input_mode = excluded.input_mode,
                updated_at = excluded.updated_at
            `,
      workerId,
      mode,
      now,
    );
  }

  /**
   * Record one thing that happened, and trim the oldest away.
   *
   * Sequence numbers are per session and never reused: a browser asks "what
   * have I missed since N", so a number that went backwards would replay
   * events it had already seen or skip ones it had not.
   *
   * Trimming keeps the newest. A session running for hours would otherwise
   * grow without bound in a Durable Object's storage, and it is the recent
   * events a reconnecting browser needs. The cut is scoped to one session, so
   * a busy one cannot trim a quiet one's history away.
   */
  appendEvent(workerId: string, eventType: string, payload: unknown): SessionEvent {
    const seq = this.currentEventSeq(workerId) + 1;
    const ts = this.#now();
    this.#rows(
      `
            INSERT INTO session_events(worker_id, seq, ts, event_type, payload_json)
            VALUES(?, ?, ?, ?, ?)
            `,
      workerId,
      seq,
      ts,
      eventType,
      JSON.stringify(payload),
    );
    this.#rows(
      `
            INSERT INTO session_state(worker_id, event_seq, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                event_seq = excluded.event_seq,
                updated_at = excluded.updated_at
            `,
      workerId,
      seq,
      ts,
    );
    this.#rows(
      `
            DELETE FROM session_events
            WHERE worker_id = ? AND seq <= ? - ?
            `,
      workerId,
      seq,
      this.#maxEvents,
    );
    return { seq, ts, type: eventType, data: payload };
  }

  /** The highest sequence number this session has reached. */
  currentEventSeq(workerId: string): number {
    // COALESCE guarantees a row with a number in it; only an executor that
    // returns nothing at all can leave there being no row.
    const row = this.#row("SELECT COALESCE(MAX(seq), 0) AS seq FROM session_events WHERE worker_id = ?", workerId);
    return row === undefined ? 0 : Number(row.seq);
  }

  /**
   * The oldest sequence number still held.
   *
   * A browser asking for anything below this has fallen too far behind to
   * catch up from the log, and needs a snapshot instead.
   */
  minEventSeq(workerId: string): number {
    const row = this.#row("SELECT COALESCE(MIN(seq), 0) AS seq FROM session_events WHERE worker_id = ?", workerId);
    return row === undefined ? 0 : Number(row.seq);
  }

  /** How many events this session still holds. */
  countEvents(workerId: string): number {
    const row = this.#row("SELECT COUNT(*) AS cnt FROM session_events WHERE worker_id = ?", workerId);
    return row === undefined ? 0 : Number(row.cnt);
  }

  /** Everything that happened after `seq`, oldest first. */
  listEventsSince(workerId: string, seq: number, limit = 100): SessionEvent[] {
    const rows = this.#rows(
      `
                SELECT seq, ts, event_type, payload_json
                FROM session_events
                WHERE worker_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                `,
      workerId,
      Math.trunc(seq),
      Math.trunc(limit),
    );
    return rows.map((row) => ({
      // Both NOT NULL in the schema.
      seq: Number(row.seq),
      ts: Number(row.ts),
      // NOT NULL, so there is nothing to fall back to.
      type: String(row.event_type),
      data: JSON.parse(String(supplied(row.payload_json) ?? "{}")) as unknown,
    }));
  }

  /**
   * The recording view of the log.
   *
   * With no offset this reads the *tail* — the most recent entries, returned
   * oldest-first so they play back in order. With one it reads forwards from
   * that point. The limit is clamped at both ends: a request for none would
   * return an empty recording, and one for everything would try to hold a
   * long session in memory.
   */
  listRecordingEntries(workerId: string, query: RecordingQuery = {}): RecordingEntry[] {
    const limit = Math.max(1, Math.min(Math.trunc(query.limit ?? 200), 500));
    const tail = query.offset === undefined;

    const params: unknown[] = [workerId];
    let where = "WHERE worker_id = ?";
    if (query.event !== undefined) {
      where += " AND event_type = ?";
      params.push(query.event);
    }
    params.push(limit);
    let suffix = `${tail ? "ORDER BY seq DESC" : "ORDER BY seq ASC"} LIMIT ?`;
    if (!tail) {
      suffix += " OFFSET ?";
      // Clamped as the reference clamps it. SQLite reads a negative offset as
      // none, so this changes no answer — it says that reading from before
      // the start means reading from the start.
      params.push(Math.max(0, Math.trunc(query.offset as number)));
    }

    // Every caller value goes through a placeholder; the string only stitches
    // together fragments written here.
    const rows = this.#rows(`SELECT ts, event_type, payload_json FROM session_events ${where} ${suffix}`, ...params);
    if (tail) {
      rows.reverse();
    }
    return rows.map((row) => ({
      ts: Number(row.ts),
      event: String(row.event_type),
      data: JSON.parse(String(supplied(row.payload_json) ?? "{}")) as unknown,
    }));
  }

  /** Register somewhere to send a session's events, replacing any by that id. */
  saveWebhook(
    webhookId: string,
    sessionId: string,
    url: string,
    options: { eventTypes?: string[]; pattern?: string; secret?: string } = {},
  ): void {
    this.#rows(
      `
            INSERT INTO webhooks(webhook_id, session_id, url, event_types_json, pattern, secret)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(webhook_id) DO UPDATE SET
                url = excluded.url,
                event_types_json = excluded.event_types_json,
                pattern = excluded.pattern,
                secret = excluded.secret
            `,
      webhookId,
      sessionId,
      url,
      // Absent and empty are different: no list means every event, an empty
      // one means none.
      options.eventTypes === undefined ? null : JSON.stringify(options.eventTypes),
      options.pattern ?? null,
      options.secret ?? null,
    );
  }

  /** Every webhook registered for one session. */
  loadWebhooks(sessionId: string): WebhookRecord[] {
    const rows = this.#rows(
      `
                SELECT webhook_id, session_id, url, event_types_json, pattern, secret
                FROM webhooks
                WHERE session_id = ?
                `,
      sessionId,
    );
    return rows.map((row) => ({
      webhook_id: String(row.webhook_id),
      session_id: String(row.session_id),
      url: String(row.url),
      event_types: JSON.parse(String(supplied(row.event_types_json) ?? "null")) as unknown,
      pattern: row.pattern ?? null,
      secret: row.secret ?? null,
    }));
  }

  /**
   * Remove a webhook.
   *
   * @returns Whether there was one to remove, so a caller can answer "no such
   *   webhook" rather than reporting a success that did nothing.
   */
  deleteWebhook(webhookId: string): boolean {
    if (this.#row("SELECT webhook_id FROM webhooks WHERE webhook_id = ?", webhookId) === undefined) {
      return false;
    }
    this.#rows("DELETE FROM webhooks WHERE webhook_id = ?", webhookId);
    return true;
  }

  /** Mint a token that lets a browser pick this session back up. */
  createResumeToken(token: string, workerId: string, role: string, ttlSeconds: number): void {
    const now = this.#now();
    this.#rows(
      `
            INSERT INTO resume_tokens(token, worker_id, role, was_hijack_owner, created_at, expires_at)
            VALUES(?, ?, ?, 0, ?, ?)
            `,
      token,
      workerId,
      role,
      now,
      now + ttlSeconds,
    );
  }

  /**
   * Read a resume token, if it is still good.
   *
   * An expired one is removed on the way out rather than merely refused, so a
   * token that has lapsed cannot be used and does not linger.
   */
  getResumeToken(token: string): ResumeTokenRecord | undefined {
    const row = this.#row(
      "SELECT token, worker_id, role, was_hijack_owner, created_at, expires_at FROM resume_tokens WHERE token = ?",
      token,
    );
    if (row === undefined) {
      return undefined;
    }
    const expiresAt = Number(row.expires_at);
    if (this.#now() > expiresAt) {
      this.revokeResumeToken(token);
      return undefined;
    }
    return {
      token: row.token,
      worker_id: row.worker_id,
      // The least privileged reading of a row that does not say.
      role: String(supplied(row.role) ?? "viewer"),
      was_hijack_owner: Boolean(Number(row.was_hijack_owner)),
      created_at: Number(row.created_at),
      expires_at: expiresAt,
    };
  }

  /** Record whether this token's holder had the keyboard. */
  markResumeHijackOwner(token: string, isOwner: boolean): void {
    this.#rows("UPDATE resume_tokens SET was_hijack_owner = ? WHERE token = ?", isOwner ? 1 : 0, token);
  }

  /** Invalidate a resume token. */
  revokeResumeToken(token: string): void {
    this.#rows("DELETE FROM resume_tokens WHERE token = ?", token);
  }

  /**
   * Remove every lapsed token.
   *
   * Returns zero rather than a count: not every executor reports how many
   * rows a delete touched, and a number that was sometimes right would be
   * worse than one that is always the same.
   */
  cleanupExpiredTokens(): number {
    this.#rows("DELETE FROM resume_tokens WHERE expires_at <= ?", this.#now());
    return 0;
  }

  /** Record the last screen, so a reconnecting browser has something to draw. */
  saveSnapshot(workerId: string, snapshot: Record<string, unknown>): void {
    const now = this.#now();
    this.#rows(
      `
            INSERT INTO session_state(worker_id, last_snapshot_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                last_snapshot_json = excluded.last_snapshot_json,
                updated_at = excluded.updated_at
            `,
      workerId,
      JSON.stringify(snapshot),
      now,
    );
  }
}
