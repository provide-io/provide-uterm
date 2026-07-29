//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { golden, NOW, rowCount, store } from "../testing/cf-store-harness.ts";
import type { SqliteStateStore } from "./index.ts";

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
