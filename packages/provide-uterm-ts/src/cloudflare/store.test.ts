//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { golden, NOW, rowCount, store, tables, withoutDeleted } from "../testing/cf-store-harness.ts";
import { type SessionMetaRecord, type SqlExecutor, SqliteStateStore } from "./index.ts";

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

  it("records which connection acquired the lease when the caller says", () => {
    // The reference's LeaseRecord carries an optional acquired_by; a clear
    // wipes it along with the rest of the lease.
    const { subject } = store();
    subject.saveLease({ workerId: "w1", hijackId: "h1", owner: "alice", leaseExpiresAt: NOW + 60, acquiredBy: "c1" });
    expect(subject.loadSession("w1")?.acquired_by).toBe("c1");
    subject.clearLease("w1");
    expect(subject.loadSession("w1")?.acquired_by).toBeNull();
  });

  it("reads a worker generation something else wrote", () => {
    // Nothing here writes the generation yet; the Python Worker sharing the
    // same storage does, and a delete must wipe it with the rest.
    const { subject, db } = store();
    db.prepare("INSERT INTO session_state(worker_id,worker_generation,updated_at) VALUES(?,?,?)").run("w1", "g7", 0);
    expect(subject.loadSession("w1")?.worker_generation).toBe("g7");
    subject.markDeleted("w1");
    expect(subject.loadSession("w1")?.worker_generation).toBeNull();
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
