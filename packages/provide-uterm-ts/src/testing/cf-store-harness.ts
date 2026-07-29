//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What the SQLite state-store suites are built from.
 *
 * The store's tests outgrew one file — the 777-line cap is per file — so
 * the corpus shape and the four helpers live here rather than being copied
 * into each half, where they would drift apart one edit at a time.
 *
 * `src/testing/` is excluded from coverage: it is scaffolding, not subject.
 */

import { DatabaseSync } from "node:sqlite";
import {
  type SessionMetaRecord,
  type SessionStateRecord,
  type SqlExecutor,
  SqliteStateStore,
} from "../cloudflare/index.ts";
import { loadGolden } from "./golden.ts";

export interface StoreGolden {
  tables: string[];
  columns: Record<string, string[]>;
  meta: Array<{
    name: string;
    meta: Record<string, unknown>;
    loaded: Record<string, unknown> | null;
    created_at: number | null;
    rows: number;
  }>;
  meta_upsert: { loaded: SessionMetaRecord; rows: number; missing: null };
  invite_state: {
    missing: null;
    first: Record<string, unknown>;
    after_second_save: Record<string, unknown>;
    rows: number;
    corrupt: null;
    not_an_object: null;
  };
  events: {
    empty_seq: number;
    empty_min: number;
    empty_count: number;
    empty_list: unknown[];
    first: { seq: number; type: string; data: unknown };
    second: { seq: number; type: string; data: unknown };
    third: { seq: number; type: string; data: unknown };
    other_session_first: { seq: number; type: string; data: unknown };
    listed: Array<{ seq: number; type: string; data: unknown }>;
    since_first: number[];
    since_last: number[];
    limited: number[];
    beyond: unknown[];
    count: number;
    min: number;
    seq: number;
    session_row_seq: number;
    trimmed_kept: number[];
    trimmed_min: number;
    trimmed_seq: number;
    trimmed_count: number;
    other_survives: number;
    rows_after_trim: number;
  };
  webhooks: {
    empty: unknown[];
    minimal: Array<Record<string, unknown>>;
    both: Array<Record<string, unknown>>;
    other_session: Array<Record<string, unknown>>;
    after_update: Array<Record<string, unknown>>;
    deleted: boolean;
    deleted_again: boolean;
    missing: boolean;
    remaining: Array<Record<string, unknown>>;
  };
  resume_tokens: {
    missing: null;
    live: Record<string, unknown>;
    owned_flag: boolean;
    disowned_flag: boolean;
    revoked: null;
    expired: null;
    expired_row_removed: boolean;
    blank_role: string;
    cleanup_returns: number;
    cleanup_leaves: number;
    cleanup_kept_live: boolean;
  };
  recording: {
    empty: unknown[];
    tail: number[];
    tail_limited: number[];
    from_start: number[];
    offset_one: number[];
    filtered: number[];
    filtered_tail: number[];
    over_limit: number;
    under_limit: number[];
    negative_offset: number[];
    shape: Record<string, unknown>;
  };
  session_state: {
    missing: null;
    after_lease: SessionStateRecord;
    after_snapshot: SessionStateRecord;
    after_mode: SessionStateRecord;
    after_clear: Record<string, unknown>;
    after_delete_has_timestamp: boolean;
    after_delete_keeps_row: boolean;
    fresh_worker: SessionStateRecord;
    mode_only: SessionStateRecord;
    snapshot_only: SessionStateRecord;
  };
}

export const golden = loadGolden<StoreGolden>("cfstore_golden.json");

/** The instant the corpus was recorded at. */
export const NOW = 1_760_000_000.0;

/** A migrated store over an in-memory database, and the database itself. */
export function store(options: { now?: () => number; maxEventsPerWorker?: number } = {}): {
  subject: SqliteStateStore;
  db: DatabaseSync;
} {
  const db = new DatabaseSync(":memory:");
  const exec: SqlExecutor = (sql, ...params) => {
    const statement = db.prepare(sql);
    // A statement that returns no rows cannot be read with `all()`.
    return statement.all(...(params as never[])) as Array<Record<string, unknown>>;
  };
  const subject = new SqliteStateStore(exec, { now: () => NOW, ...options });
  subject.migrate();
  return { subject, db };
}

/** Every table in a database, sorted. */
export function tables(db: DatabaseSync): string[] {
  return (
    db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as Array<{ name: string }>
  ).map((row) => row.name);
}

/** How many rows a table holds. */
export function rowCount(db: DatabaseSync, table: string): number {
  return Number((db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number }).n);
}

/** A session without its deletion timestamp, which is a wall clock. */
export function withoutDeleted(session: SessionStateRecord | undefined): Record<string, unknown> | null {
  if (session === undefined) {
    return null;
  }
  const { deleted_at, ...rest } = session;
  return rest;
}
