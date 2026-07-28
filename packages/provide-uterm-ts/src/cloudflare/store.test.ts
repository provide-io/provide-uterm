//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type SessionMetaRecord, type SessionStateRecord, type SqlExecutor, SqliteStateStore } from "./index.ts";

interface StoreGolden {
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

const golden = loadGolden<StoreGolden>("cfstore_golden.json");

/** The instant the corpus was recorded at. */
const NOW = 1_760_000_000.0;

/** A migrated store over an in-memory database, and the database itself. */
function store(options: { now?: () => number; maxEventsPerWorker?: number } = {}): {
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
function tables(db: DatabaseSync): string[] {
  return (
    db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as Array<{ name: string }>
  ).map((row) => row.name);
}

/** How many rows a table holds. */
function rowCount(db: DatabaseSync, table: string): number {
  return Number((db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number }).n);
}

/** A session without its deletion timestamp, which is a wall clock. */
function withoutDeleted(session: SessionStateRecord | undefined): Record<string, unknown> | null {
  if (session === undefined) {
    return null;
  }
  const { deleted_at, ...rest } = session;
  return rest;
}

describe("setting up the tables", () => {
  it("creates everything a session needs", () => {
    const { db } = store();
    expect(tables(db)).toStrictEqual(golden.tables);
  });

  it("gives each table the columns the reference gives it", () => {
    // The Durable Object and the Python Worker may run against the same
    // storage, so the shapes have to agree.
    const { db } = store();
    for (const table of golden.tables) {
      const columns = (db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>).map(
        (row) => row.name,
      );
      expect(columns).toStrictEqual(golden.columns[table]);
    }
  });

  it("can run again without complaint", () => {
    // It runs on every Durable Object start, and every start after the first
    // finds the tables and the later columns already there.
    const { subject, db } = store();
    expect(() => subject.migrate()).not.toThrow();
    expect(() => subject.migrate()).not.toThrow();
    expect(tables(db)).toStrictEqual(golden.tables);
  });

  it("adds the later columns to a table that predates them", () => {
    // A store created before those columns existed must gain them rather than
    // failing to open — losing the session over a column would be worse than
    // losing the column.
    const db = new DatabaseSync(":memory:");
    db.exec(
      `CREATE TABLE session_state (worker_id TEXT PRIMARY KEY, hijack_id TEXT, owner TEXT,
       lease_expires_at REAL, last_snapshot_json TEXT, event_seq INTEGER NOT NULL DEFAULT 0,
       updated_at REAL NOT NULL)`,
    );
    const exec: SqlExecutor = (sql, ...params) =>
      db.prepare(sql).all(...(params as never[])) as Array<Record<string, unknown>>;
    new SqliteStateStore(exec).migrate();
    const columns = (db.prepare("PRAGMA table_info(session_state)").all() as Array<{ name: string }>).map(
      (row) => row.name,
    );
    expect(columns).toContain("input_mode");
    expect(columns).toContain("deleted_at");
  });
});

describe("what a session says about itself", () => {
  it.each(golden.meta)("$name", (record) => {
    const { subject, db } = store();
    subject.saveSessionMeta("w1", record.meta);
    const loaded = subject.loadSessionMeta("w1");
    const { created_at, ...rest } = loaded as SessionMetaRecord;
    expect(rest).toStrictEqual(record.loaded);
    if (record.created_at !== null) {
      expect(created_at).toBe(record.created_at);
    }
    expect(rowCount(db, "session_meta")).toBe(record.rows);
  });

  it("fills in what was not supplied", () => {
    // A fleet list with a blank name in it is a list nobody can read.
    const record = golden.meta.find((entry) => entry.name === "nothing supplied");
    expect(record?.loaded?.display_name).toBe("w1");
    expect(record?.loaded?.connector_type).toBe("unknown");
    expect(record?.loaded?.visibility).toBe("public");
    expect(record?.loaded?.tags).toStrictEqual([]);
    expect(record?.loaded?.owner).toBeNull();
  });

  it("treats an empty string as nothing supplied", () => {
    const record = golden.meta.find((entry) => entry.name === "empty strings");
    expect(record?.loaded?.display_name).toBe("w1");
    expect(record?.loaded?.connector_type).toBe("unknown");
    expect(record?.loaded?.visibility).toBe("public");
  });

  it("stamps a created time when there is none", () => {
    // So a session always has an age, even one whose caller did not say.
    const { subject } = store();
    subject.saveSessionMeta("w1", {});
    expect(subject.loadSessionMeta("w1")?.created_at).toBe(NOW);
  });

  it("stamps one when the caller said zero", () => {
    // Zero is not a time a session was created at.
    const { subject } = store();
    subject.saveSessionMeta("w1", { created_at: 0 });
    expect(subject.loadSessionMeta("w1")?.created_at).toBe(NOW);
  });

  it("keeps text as it was written", () => {
    expect(golden.meta.find((entry) => entry.name === "unicode in the name")?.loaded?.display_name).toBe("héllo → ✓");
  });

  it("replaces rather than accumulates", () => {
    // A Durable Object rewrites its own row on every restart. A second row
    // for one session would make the fleet list ambiguous.
    const { subject, db } = store();
    subject.saveSessionMeta("w1", { display_name: "first", created_at: 1.0 });
    subject.saveSessionMeta("w1", { display_name: "second", created_at: 2.0 });
    subject.saveSessionMeta("w2", { display_name: "other", created_at: 3.0 });
    expect(subject.loadSessionMeta("w1")).toStrictEqual(golden.meta_upsert.loaded);
    expect(rowCount(db, "session_meta")).toBe(golden.meta_upsert.rows);
  });

  it("says nothing about a session it has never seen", () => {
    expect(store().subject.loadSessionMeta("nobody")).toBeUndefined();
  });

  it("writes the filled-in values, not the blanks it was given", () => {
    // The load side fills in too, so the two would mask each other. This
    // reads the row to show the blank never reached the database.
    const { subject, db } = store();
    subject.saveSessionMeta("w1", { display_name: "", connector_type: "", visibility: "" });
    const row = db
      .prepare("SELECT display_name, connector_type, visibility FROM session_meta WHERE worker_id=?")
      .get("w1") as Record<string, unknown>;
    expect(row.display_name).toBe("w1");
    expect(row.connector_type).toBe("unknown");
    expect(row.visibility).toBe("public");
  });

  it("fills in a row that was written without going through it", () => {
    // A row left by an older build, or by the Python Worker sharing the same
    // storage, may hold blanks this never writes.
    const { subject, db } = store();
    db.prepare(
      "INSERT INTO session_meta(worker_id,display_name,connector_type,created_at,tags_json,visibility,owner) VALUES(?,?,?,?,?,?,?)",
    ).run("w1", "", "", 0, "", "", null);
    expect(subject.loadSessionMeta("w1")).toStrictEqual({
      display_name: "w1",
      connector_type: "unknown",
      created_at: 0,
      tags: [],
      visibility: "public",
      owner: null,
    });
  });
});

describe("consumed invite digests", () => {
  it("remembers what was redeemed", () => {
    // A one-time invite is only one-time if the redemption survives a
    // restart.
    const { subject, db } = store();
    expect(subject.loadTunnelInviteState("w1")).toBeUndefined();
    subject.saveTunnelInviteState("w1", { share: "sha256:abc", consumed_at: 12.0 });
    expect(subject.loadTunnelInviteState("w1")).toStrictEqual(golden.invite_state.first);
    subject.saveTunnelInviteState("w1", { share: "sha256:def" });
    expect(subject.loadTunnelInviteState("w1")).toStrictEqual(golden.invite_state.after_second_save);
    expect(rowCount(db, "tunnel_invite_state")).toBe(golden.invite_state.rows);
  });

  it("says nothing about an entry it cannot read", () => {
    // Corrupt or of the wrong shape reads as "nothing redeemed", which is the
    // safe direction: an invite may be offered again rather than a session
    // being locked out of its own tunnel.
    const { subject, db } = store();
    db.prepare("INSERT INTO tunnel_invite_state(worker_id,entry_json,updated_at) VALUES(?,?,?)").run(
      "w1",
      "{not json",
      0,
    );
    expect(subject.loadTunnelInviteState("w1")).toBeUndefined();

    const other = store();
    other.db
      .prepare("INSERT INTO tunnel_invite_state(worker_id,entry_json,updated_at) VALUES(?,?,?)")
      .run("w1", '["a"]', 0);
    expect(other.subject.loadTunnelInviteState("w1")).toBeUndefined();
  });
});

describe("a session's own state", () => {
  it("says nothing about one it has never seen", () => {
    expect(store().subject.loadSession("w1")).toBeUndefined();
  });

  it("records who holds the lease and until when", () => {
    const { subject } = store();
    subject.saveLease({ workerId: "w1", hijackId: "h1", owner: "alice", leaseExpiresAt: NOW + 60 });
    expect(subject.loadSession("w1")).toStrictEqual(golden.session_state.after_lease);
  });

  it("creates the row when there is not one", () => {
    const { subject } = store();
    subject.saveLease({ workerId: "w2", hijackId: "h2", owner: "bob", leaseExpiresAt: NOW + 30 });
    expect(subject.loadSession("w2")).toStrictEqual(golden.session_state.fresh_worker);
  });

  it("keeps the snapshot and the mode alongside the lease", () => {
    const { subject } = store();
    subject.saveLease({ workerId: "w1", hijackId: "h1", owner: "alice", leaseExpiresAt: NOW + 60 });
    subject.saveSnapshot("w1", { screen: "hello", cols: 80 });
    expect(subject.loadSession("w1")).toStrictEqual(golden.session_state.after_snapshot);
    subject.saveInputMode("w1", "observe");
    expect(subject.loadSession("w1")).toStrictEqual(golden.session_state.after_mode);
  });

  it("clears every part of a lease at once", () => {
    // A half-cleared lease leaves an owner with no expiry, which reads as one
    // that never ends.
    const { subject } = store();
    subject.saveLease({ workerId: "w1", hijackId: "h1", owner: "alice", leaseExpiresAt: NOW + 60 });
    subject.saveSnapshot("w1", { screen: "hello", cols: 80 });
    subject.saveInputMode("w1", "observe");
    subject.clearLease("w1");
    expect(withoutDeleted(subject.loadSession("w1"))).toStrictEqual(golden.session_state.after_clear);
  });

  it("leaves the snapshot alone when clearing a lease", () => {
    // Giving up the keyboard is not losing the screen.
    expect(golden.session_state.after_clear.last_snapshot).toStrictEqual({ screen: "hello", cols: 80 });
    expect(golden.session_state.after_clear.input_mode).toBe("observe");
  });

  it("marks a deleted session rather than removing it", () => {
    // A request arriving afterwards is told the session is gone rather than
    // that it never existed.
    const { subject, db } = store();
    subject.saveLease({ workerId: "w1", hijackId: "h1", owner: "alice", leaseExpiresAt: NOW + 60 });
    subject.markDeleted("w1");
    const after = subject.loadSession("w1");
    expect(after?.deleted_at).toBe(NOW);
    expect(rowCount(db, "session_state")).toBe(1);
    expect(golden.session_state.after_delete_has_timestamp).toBe(true);
    expect(golden.session_state.after_delete_keeps_row).toBe(true);
  });

  it("clears the lease and the snapshot when marking a deletion", () => {
    // Nothing of a deleted session should be readable but the fact of it.
    const { subject } = store();
    subject.saveLease({ workerId: "w1", hijackId: "h1", owner: "alice", leaseExpiresAt: NOW + 60 });
    subject.saveSnapshot("w1", { screen: "hello" });
    subject.markDeleted("w1");
    const after = subject.loadSession("w1");
    expect(after?.hijack_id).toBeNull();
    expect(after?.owner).toBeNull();
    expect(after?.lease_expires_at).toBeNull();
    expect(after?.last_snapshot).toBeNull();
  });

  it("marks one it has never seen", () => {
    // A delete for a session this object never held still leaves a tombstone,
    // so the next request is answered rather than starting it up again.
    const { subject } = store();
    subject.markDeleted("w9");
    expect(subject.loadSession("w9")?.deleted_at).toBe(NOW);
  });

  it("records a mode for a session with no row yet", () => {
    const { subject } = store();
    subject.saveInputMode("w3", "observe");
    expect(subject.loadSession("w3")).toStrictEqual(golden.session_state.mode_only);
  });

  it("records a snapshot for a session with no row yet", () => {
    const { subject } = store();
    subject.saveSnapshot("w4", { screen: "x" });
    expect(subject.loadSession("w4")).toStrictEqual(golden.session_state.snapshot_only);
  });

  it("defaults the mode when none was set", () => {
    expect(golden.session_state.snapshot_only.input_mode).toBe("hijack");
  });

  it("clears one session's lease and no other's", () => {
    // Two sessions share one Durable Object's storage in tests and one
    // object's history; a clear that matched every row would hand the
    // keyboard back on all of them.
    const { subject } = store();
    subject.saveLease({ workerId: "w1", hijackId: "h1", owner: "alice", leaseExpiresAt: NOW + 60 });
    subject.saveLease({ workerId: "w2", hijackId: "h2", owner: "bob", leaseExpiresAt: NOW + 60 });
    subject.clearLease("w1");
    expect(subject.loadSession("w1")?.owner).toBeNull();
    expect(subject.loadSession("w2")?.owner).toBe("bob");
  });

  it("reads the event sequence a session has reached", () => {
    // Nothing here advances it yet — appending events is the next piece — so
    // the row is written directly. A reconnecting browser asks "what have I
    // missed since", and this number is the answer.
    const { subject, db } = store();
    db.prepare("INSERT INTO session_state(worker_id,event_seq,updated_at) VALUES(?,?,?)").run("w1", 5, 0);
    expect(subject.loadSession("w1")?.event_seq).toBe(5);
  });

  it("reads a session row that holds blanks", () => {
    // As above: written by something else against the same storage.
    const { subject, db } = store();
    db.prepare(
      "INSERT INTO session_state(worker_id,last_snapshot_json,event_seq,updated_at,input_mode) VALUES(?,?,?,?,?)",
    ).run("w1", "", 0, 0, "");
    const session = subject.loadSession("w1");
    expect(session?.last_snapshot).toBeNull();
    expect(session?.event_seq).toBe(0);
    expect(session?.input_mode).toBe("hijack");
  });
});

describe("how many events a session may keep", () => {
  it("takes the ceiling it was given", () => {
    const db = new DatabaseSync(":memory:");
    const exec: SqlExecutor = (sql, ...params) =>
      db.prepare(sql).all(...(params as never[])) as Array<Record<string, unknown>>;
    expect(new SqliteStateStore(exec, { maxEventsPerWorker: 10 }).maxEvents).toBe(10);
  });

  it("defaults to two thousand", () => {
    const db = new DatabaseSync(":memory:");
    const exec: SqlExecutor = (sql, ...params) =>
      db.prepare(sql).all(...(params as never[])) as Array<Record<string, unknown>>;
    expect(new SqliteStateStore(exec).maxEvents).toBe(2000);
  });

  it("reads the wall clock when it is not given one", () => {
    // Which is what it does in production; the tests pin a clock so the
    // recorded timestamps hold still.
    const db = new DatabaseSync(":memory:");
    const exec: SqlExecutor = (sql, ...params) =>
      db.prepare(sql).all(...(params as never[])) as Array<Record<string, unknown>>;
    const subject = new SqliteStateStore(exec);
    subject.migrate();
    const before = Date.now() / 1000;
    subject.saveSessionMeta("w1", {});
    expect(subject.loadSessionMeta("w1")?.created_at).toBeGreaterThanOrEqual(before);
  });

  it("copes with an executor that returns nothing", () => {
    // A write statement produces no rows, and some drivers say so by
    // returning nothing at all rather than an empty list.
    const exec: SqlExecutor = () => undefined;
    const subject = new SqliteStateStore(exec);
    expect(() => subject.migrate()).not.toThrow();
    expect(subject.loadSession("w1")).toBeUndefined();
    expect(subject.loadSessionMeta("w1")).toBeUndefined();
    expect(subject.loadTunnelInviteState("w1")).toBeUndefined();
    // The event reads answer from a row that COALESCE always produces, so an
    // executor returning nothing is the only way there is no row at all.
    expect(subject.currentEventSeq("w1")).toBe(0);
    expect(subject.minEventSeq("w1")).toBe(0);
    expect(subject.countEvents("w1")).toBe(0);
    expect(subject.listEventsSince("w1", 0)).toStrictEqual([]);
  });

  it("keeps at least one", () => {
    // A store that could hold no events would drop each as it arrived and
    // leave a reconnecting browser nothing to catch up on.
    const db = new DatabaseSync(":memory:");
    const exec: SqlExecutor = (sql, ...params) =>
      db.prepare(sql).all(...(params as never[])) as Array<Record<string, unknown>>;
    expect(new SqliteStateStore(exec, { maxEventsPerWorker: 0 }).maxEvents).toBe(1);
    expect(new SqliteStateStore(exec, { maxEventsPerWorker: -5 }).maxEvents).toBe(1);
  });
});

describe("the event log", () => {
  /** An event without its timestamp, which is a wall clock. */
  function withoutTs(event: { seq: number; ts: number; type: string; data: unknown }) {
    const { ts, ...rest } = event;
    return rest;
  }

  it("says nothing about a session with no events", () => {
    const { subject } = store();
    expect(subject.currentEventSeq("w1")).toBe(golden.events.empty_seq);
    expect(subject.minEventSeq("w1")).toBe(golden.events.empty_min);
    expect(subject.countEvents("w1")).toBe(golden.events.empty_count);
    expect(subject.listEventsSince("w1", 0)).toStrictEqual(golden.events.empty_list);
  });

  it("numbers events from one, upwards", () => {
    // A browser asks "what have I missed since N". A number that went
    // backwards would replay events it had seen or skip ones it had not.
    const { subject } = store();
    expect(withoutTs(subject.appendEvent("w1", "output", { text: "hello" }))).toStrictEqual(golden.events.first);
    expect(withoutTs(subject.appendEvent("w1", "output", { text: "world" }))).toStrictEqual(golden.events.second);
    expect(withoutTs(subject.appendEvent("w1", "resize", { cols: 80, rows: 24 }))).toStrictEqual(golden.events.third);
  });

  it("numbers each session separately", () => {
    // Two sessions share one object's storage; a shared counter would make
    // one session's catch-up point meaningless to the other.
    const { subject } = store();
    subject.appendEvent("w1", "output", { text: "hello" });
    subject.appendEvent("w1", "output", { text: "world" });
    expect(withoutTs(subject.appendEvent("w2", "output", { text: "other" }))).toStrictEqual(
      golden.events.other_session_first,
    );
  });

  it("lists one session's events and no other's", () => {
    // Two sessions share one object's storage. A listing that crossed them
    // would replay another session's output into this one's terminal.
    const { subject } = store();
    subject.appendEvent("w1", "output", { text: "mine" });
    subject.appendEvent("w2", "output", { text: "theirs" });
    subject.appendEvent("w2", "output", { text: "theirs again" });
    expect(subject.listEventsSince("w1", 0).map((event) => event.data)).toStrictEqual([{ text: "mine" }]);
  });

  it("reads a fractional point as a whole one", () => {
    // The sequence is a count of events; asking from half of one is asking
    // from the one before it.
    const { subject } = store();
    for (const text of ["a", "b", "c"]) {
      subject.appendEvent("w1", "output", { text });
    }
    expect(subject.listEventsSince("w1", 1.9).map((event) => event.seq)).toStrictEqual([2, 3]);
    expect(subject.listEventsSince("w1", 0, 2.9).map((event) => event.seq)).toStrictEqual([1, 2]);
  });

  it("stamps each event with a time", () => {
    const { subject } = store();
    expect(subject.appendEvent("w1", "output", {}).ts).toBe(NOW);
  });

  it("hands the stamped time back when listing", () => {
    // The time is how a replay paces itself; a listing that lost it would
    // play a session back with no timing at all.
    const { subject } = store();
    subject.appendEvent("w1", "output", {});
    expect(subject.listEventsSince("w1", 0)[0]?.ts).toBe(NOW);
  });

  it("records the sequence on the session row too", () => {
    // Which is what a reconnecting browser reads before asking for anything,
    // so the two must not disagree.
    const { subject } = store();
    for (const text of ["a", "b", "c"]) {
      subject.appendEvent("w1", "output", { text });
    }
    expect(subject.loadSession("w1")?.event_seq).toBe(golden.events.session_row_seq);
  });

  it("hands back everything since a point, oldest first", () => {
    // Oldest first, because they are replayed in order into a terminal.
    const { subject } = store();
    subject.appendEvent("w1", "output", { text: "hello" });
    subject.appendEvent("w1", "output", { text: "world" });
    subject.appendEvent("w1", "resize", { cols: 80, rows: 24 });
    expect(subject.listEventsSince("w1", 0).map(withoutTs)).toStrictEqual(golden.events.listed);
    expect(subject.listEventsSince("w1", 1).map((event) => event.seq)).toStrictEqual(golden.events.since_first);
  });

  it("hands back nothing when there is nothing newer", () => {
    const { subject } = store();
    subject.appendEvent("w1", "output", {});
    expect(subject.listEventsSince("w1", 1)).toStrictEqual([]);
    expect(golden.events.since_last).toStrictEqual([]);
    expect(golden.events.beyond).toStrictEqual([]);
  });

  it("honours the limit it was given", () => {
    // A browser catching up on a long session takes it a page at a time.
    const { subject } = store();
    for (const text of ["a", "b", "c"]) {
      subject.appendEvent("w1", "output", { text });
    }
    expect(subject.listEventsSince("w1", 0, 2).map((event) => event.seq)).toStrictEqual(golden.events.limited);
  });

  it("defaults the limit rather than returning everything", () => {
    const { subject } = store({ maxEventsPerWorker: 500 });
    for (let index = 0; index < 150; index += 1) {
      subject.appendEvent("w1", "output", { index });
    }
    expect(subject.listEventsSince("w1", 0)).toHaveLength(100);
  });

  it("keeps only the newest when it runs out of room", () => {
    // A session running for hours would otherwise grow without bound in a
    // Durable Object's storage, and it is the recent events a reconnecting
    // browser needs.
    const { subject } = store({ maxEventsPerWorker: 3 });
    for (let index = 0; index < 6; index += 1) {
      subject.appendEvent("t1", "output", { n: index });
    }
    expect(subject.listEventsSince("t1", 0).map((event) => event.seq)).toStrictEqual(golden.events.trimmed_kept);
    expect(subject.countEvents("t1")).toBe(golden.events.trimmed_count);
  });

  it("keeps counting up after trimming", () => {
    // The sequence is a position in the session's history, not an index into
    // what is still stored.
    const { subject } = store({ maxEventsPerWorker: 3 });
    for (let index = 0; index < 6; index += 1) {
      subject.appendEvent("t1", "output", { n: index });
    }
    expect(subject.currentEventSeq("t1")).toBe(golden.events.trimmed_seq);
    expect(subject.minEventSeq("t1")).toBe(golden.events.trimmed_min);
  });

  it("says how far back it can still reach", () => {
    // A browser asking for anything below this has fallen too far behind to
    // catch up from the log and needs a snapshot instead.
    const { subject } = store({ maxEventsPerWorker: 3 });
    for (let index = 0; index < 6; index += 1) {
      subject.appendEvent("t1", "output", { n: index });
    }
    expect(subject.minEventSeq("t1")).toBe(4);
    expect(subject.listEventsSince("t1", 0)).toHaveLength(3);
  });

  it("trims one session without touching another", () => {
    // A busy session must not trim a quiet one's history away.
    const { subject } = store({ maxEventsPerWorker: 3 });
    subject.appendEvent("t2", "output", { n: 0 });
    for (let index = 0; index < 6; index += 1) {
      subject.appendEvent("t1", "output", { n: index });
    }
    expect(subject.countEvents("t2")).toBe(golden.events.other_survives);
  });

  it("keeps exactly the room it was given", () => {
    // The boundary: with room for three and three events, nothing is cut.
    const { subject } = store({ maxEventsPerWorker: 3 });
    for (let index = 0; index < 3; index += 1) {
      subject.appendEvent("t1", "output", { n: index });
    }
    expect(subject.countEvents("t1")).toBe(3);
    subject.appendEvent("t1", "output", { n: 3 });
    expect(subject.countEvents("t1")).toBe(3);
    expect(subject.minEventSeq("t1")).toBe(2);
  });

  it("keeps one when told to keep none", () => {
    const { subject } = store({ maxEventsPerWorker: 0 });
    subject.appendEvent("t1", "output", { n: 0 });
    subject.appendEvent("t1", "output", { n: 1 });
    expect(subject.countEvents("t1")).toBe(1);
  });

  it("reads an event whose payload is empty", () => {
    const { subject, db } = store();
    db.prepare("INSERT INTO session_events(worker_id,seq,ts,event_type,payload_json) VALUES(?,?,?,?,?)").run(
      "w1",
      1,
      0,
      "",
      "",
    );
    expect(subject.listEventsSince("w1", 0)).toStrictEqual([{ seq: 1, ts: 0, type: "", data: {} }]);
  });
});

describe("webhooks", () => {
  /** The registrations for a session, in a stable order. */
  function sorted(subject: SqliteStateStore, sessionId: string) {
    return [...subject.loadWebhooks(sessionId)].sort((a, b) => a.webhook_id.localeCompare(b.webhook_id));
  }

  it("says nothing about a session with none", () => {
    expect(store().subject.loadWebhooks("s1")).toStrictEqual(golden.webhooks.empty);
  });

  it("records one with nothing but a url", () => {
    const { subject } = store();
    subject.saveWebhook("h1", "s1", "https://example/hook");
    expect(subject.loadWebhooks("s1")).toStrictEqual(golden.webhooks.minimal);
  });

  it("tells an absent event list from an empty one", () => {
    // No list means every event; an empty one means none. Collapsing them
    // would either silence a webhook or make it fire on everything.
    const { subject } = store();
    subject.saveWebhook("h1", "s1", "https://example/hook");
    expect(subject.loadWebhooks("s1")[0]?.event_types).toBeNull();
    subject.saveWebhook("h2", "s1", "https://example/other", { eventTypes: [] });
    expect(sorted(subject, "s1")[1]?.event_types).toStrictEqual([]);
  });

  it("records everything a registration can carry", () => {
    const { subject } = store();
    subject.saveWebhook("h1", "s1", "https://example/hook");
    subject.saveWebhook("h2", "s1", "https://example/other", {
      eventTypes: ["output", "exit"],
      pattern: "ERROR",
      secret: "sh", // pragma: allowlist secret - a fixture, never a credential
    });
    expect(sorted(subject, "s1")).toStrictEqual(golden.webhooks.both);
  });

  it("keeps each session's registrations apart", () => {
    // A webhook is somewhere a session's output is sent. Crossing them would
    // deliver one session's terminal to another's endpoint.
    const { subject } = store();
    subject.saveWebhook("h1", "s1", "https://example/hook");
    subject.saveWebhook("h3", "s2", "https://example/theirs");
    expect(subject.loadWebhooks("s2")).toStrictEqual(golden.webhooks.other_session);
  });

  it("replaces one registered under the same id", () => {
    const { subject } = store();
    subject.saveWebhook("h1", "s1", "https://example/hook");
    subject.saveWebhook("h1", "s1", "https://example/moved", { eventTypes: [] });
    expect(subject.loadWebhooks("s1")).toStrictEqual(golden.webhooks.after_update);
    expect(subject.loadWebhooks("s1")).toHaveLength(1);
  });

  it("says whether there was one to remove", () => {
    // So a caller can answer "no such webhook" rather than report a success
    // that did nothing.
    const { subject } = store();
    subject.saveWebhook("h1", "s1", "https://example/hook");
    expect(subject.deleteWebhook("h1")).toBe(golden.webhooks.deleted);
    expect(subject.deleteWebhook("h1")).toBe(golden.webhooks.deleted_again);
    expect(subject.deleteWebhook("never")).toBe(golden.webhooks.missing);
  });

  it("removes only the one asked for", () => {
    const { subject } = store();
    subject.saveWebhook("h1", "s1", "https://example/hook");
    subject.saveWebhook("h2", "s1", "https://example/other");
    subject.deleteWebhook("h1");
    expect(subject.loadWebhooks("s1").map((hook) => hook.webhook_id)).toStrictEqual(["h2"]);
  });
});

describe("resume tokens", () => {
  it("says nothing about one it never minted", () => {
    expect(store().subject.getResumeToken("t0")).toBeUndefined();
  });

  it("hands back what it minted", () => {
    const { subject } = store();
    subject.createResumeToken("t1", "w1", "operator", 300);
    const token = subject.getResumeToken("t1");
    const { created_at, expires_at, ...rest } = token as NonNullable<typeof token>;
    expect(rest).toStrictEqual(golden.resume_tokens.live);
    expect(created_at).toBe(NOW);
    expect(expires_at).toBe(NOW + 300);
  });

  it("remembers whether its holder had the keyboard", () => {
    // A browser that resumes as the owner takes the lease straight back; one
    // that does not must not.
    const { subject } = store();
    subject.createResumeToken("t1", "w1", "operator", 300);
    subject.markResumeHijackOwner("t1", true);
    expect(subject.getResumeToken("t1")?.was_hijack_owner).toBe(golden.resume_tokens.owned_flag);
    subject.markResumeHijackOwner("t1", false);
    expect(subject.getResumeToken("t1")?.was_hijack_owner).toBe(golden.resume_tokens.disowned_flag);
  });

  it("starts a token without the keyboard", () => {
    // Minted unprivileged; the flag is set afterwards by whatever knows.
    const { subject } = store();
    subject.createResumeToken("t1", "w1", "operator", 300);
    expect(subject.getResumeToken("t1")?.was_hijack_owner).toBe(false);
  });

  it("forgets a revoked token and no other", () => {
    // Revoking one browser's resume must not lock every other browser out of
    // the session.
    const { subject } = store();
    subject.createResumeToken("t1", "w1", "operator", 300);
    subject.createResumeToken("t2", "w1", "viewer", 300);
    subject.revokeResumeToken("t1");
    expect(subject.getResumeToken("t1")).toBeUndefined();
    expect(subject.getResumeToken("t2")).toBeDefined();
  });

  it("marks one token's keyboard flag and no other's", () => {
    // The flag decides whether a resuming browser takes the lease back.
    // Setting it across the board would hand the keyboard to everyone.
    const { subject } = store();
    subject.createResumeToken("t1", "w1", "operator", 300);
    subject.createResumeToken("t2", "w1", "viewer", 300);
    subject.markResumeHijackOwner("t1", true);
    expect(subject.getResumeToken("t1")?.was_hijack_owner).toBe(true);
    expect(subject.getResumeToken("t2")?.was_hijack_owner).toBe(false);
  });

  it("refuses an expired token and removes it", () => {
    // Removed on the way out rather than merely refused, so a lapsed token
    // cannot be used and does not linger in the store.
    const { subject, db } = store();
    subject.createResumeToken("t2", "w1", "viewer", -1);
    expect(subject.getResumeToken("t2")).toBeUndefined();
    expect(rowCount(db, "resume_tokens")).toBe(0);
    expect(golden.resume_tokens.expired_row_removed).toBe(true);
  });

  it("accepts one that expires this instant", () => {
    // The check is strictly past the expiry, so a token is good up to and
    // including the moment it lapses.
    const { subject } = store();
    subject.createResumeToken("t1", "w1", "viewer", 0);
    expect(subject.getResumeToken("t1")).toBeDefined();
  });

  it("reads a row with no role as the least privileged one", () => {
    // Rather than as no role at all, which a caller might read as unchecked.
    const { subject, db } = store();
    db.prepare(
      "INSERT INTO resume_tokens(token,worker_id,role,was_hijack_owner,created_at,expires_at) VALUES(?,?,?,?,?,?)",
    ).run("t3", "w1", "", 0, 0, NOW * 2);
    expect(subject.getResumeToken("t3")?.role).toBe(golden.resume_tokens.blank_role);
  });

  it("sweeps the lapsed ones and leaves the rest", () => {
    const { subject, db } = store();
    subject.createResumeToken("keep", "w1", "viewer", 3600);
    subject.createResumeToken("drop", "w1", "viewer", -1);
    expect(subject.cleanupExpiredTokens()).toBe(golden.resume_tokens.cleanup_returns);
    expect(rowCount(db, "resume_tokens")).toBe(golden.resume_tokens.cleanup_leaves);
    expect(subject.getResumeToken("keep")).toBeDefined();
  });
});

describe("the recording view", () => {
  /** A store holding five alternating events. */
  function recorded() {
    const made = store();
    for (let index = 0; index < 5; index += 1) {
      made.subject.appendEvent("w1", index % 2 === 0 ? "output" : "resize", { n: index });
    }
    return made;
  }

  /** The indices carried by a list of entries. */
  function indices(entries: Array<{ data: unknown }>): unknown[] {
    return entries.map((entry) => (entry.data as { n: unknown }).n);
  }

  it("says nothing about a session with no events", () => {
    expect(store().subject.listRecordingEntries("w1")).toStrictEqual(golden.recording.empty);
  });

  it("reads the tail, oldest first", () => {
    // The most recent entries, in the order they happened — so they play back
    // into a terminal the way they came out of it.
    const { subject } = recorded();
    expect(indices(subject.listRecordingEntries("w1"))).toStrictEqual(golden.recording.tail);
    expect(indices(subject.listRecordingEntries("w1", { limit: 2 }))).toStrictEqual(golden.recording.tail_limited);
  });

  it("reads forwards from an offset", () => {
    const { subject } = recorded();
    expect(indices(subject.listRecordingEntries("w1", { offset: 0 }))).toStrictEqual(golden.recording.from_start);
    expect(indices(subject.listRecordingEntries("w1", { offset: 1, limit: 2 }))).toStrictEqual(
      golden.recording.offset_one,
    );
  });

  it("reads a negative offset as the start", () => {
    expect(indices(recorded().subject.listRecordingEntries("w1", { offset: -5, limit: 2 }))).toStrictEqual(
      golden.recording.negative_offset,
    );
  });

  it("filters by event type", () => {
    const { subject } = recorded();
    expect(indices(subject.listRecordingEntries("w1", { event: "output" }))).toStrictEqual(golden.recording.filtered);
    expect(indices(subject.listRecordingEntries("w1", { event: "output", limit: 1 }))).toStrictEqual(
      golden.recording.filtered_tail,
    );
  });

  it("clamps the limit at both ends", () => {
    // A request for none would return an empty recording; one for everything
    // would try to hold a long session in memory.
    const { subject } = recorded();
    expect(subject.listRecordingEntries("w1", { limit: 10_000 })).toHaveLength(golden.recording.over_limit);
    expect(indices(subject.listRecordingEntries("w1", { limit: 0 }))).toStrictEqual(golden.recording.under_limit);
  });

  it("will not hand back more than five hundred at once", () => {
    // The ceiling only bites on a session long enough to reach it, which is
    // exactly the session it exists for.
    const { subject } = store({ maxEventsPerWorker: 1000 });
    for (let index = 0; index < 600; index += 1) {
      subject.appendEvent("w1", "output", { n: index });
    }
    expect(subject.listRecordingEntries("w1", { limit: 10_000 })).toHaveLength(500);
    expect(subject.listRecordingEntries("w1", { offset: 0, limit: 10_000 })).toHaveLength(500);
  });

  it("hands back two hundred when not told a limit", () => {
    const { subject } = store({ maxEventsPerWorker: 1000 });
    for (let index = 0; index < 600; index += 1) {
      subject.appendEvent("w1", "output", { n: index });
    }
    expect(subject.listRecordingEntries("w1")).toHaveLength(200);
  });

  it("names the event rather than numbering it", () => {
    // A recording is read by a person; a sequence number means nothing to
    // them, and the type does.
    const { subject } = recorded();
    const [first] = subject.listRecordingEntries("w1");
    const { ts, ...rest } = first as NonNullable<typeof first>;
    expect(rest).toStrictEqual(golden.recording.shape);
    expect(ts).toBe(NOW);
  });

  it("reads an entry whose payload is empty", () => {
    // Written by something else against the same storage, or by an older
    // build. An unreadable entry should not break the recording around it.
    const { subject, db } = store();
    db.prepare("INSERT INTO session_events(worker_id,seq,ts,event_type,payload_json) VALUES(?,?,?,?,?)").run(
      "w1",
      1,
      0,
      "output",
      "",
    );
    expect(subject.listRecordingEntries("w1")).toStrictEqual([{ ts: 0, event: "output", data: {} }]);
  });

  it("keeps each session's recording apart", () => {
    const { subject } = recorded();
    subject.appendEvent("w2", "output", { n: 99 });
    expect(indices(subject.listRecordingEntries("w2"))).toStrictEqual([99]);
  });
});
