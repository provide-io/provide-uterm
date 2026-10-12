//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A recording never carries one session instance's entries into another's.
 *
 * A session id can be reused (a PAM capture session is named after a pid, and
 * pids are reused), so `startSession` continues the recording on file only
 * when the same instance started it, and otherwise begins afresh. Port of
 * test_recording_instances.py.
 */

import {
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type RecordingSettings, SessionRecording } from "../server/session-recording.ts";
import { InMemoryRecordingStore, LocalFileRecordingStore, type RecordingStore, retireRecording } from "./index.ts";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "uterm-rec-instance-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

async function recordOne(
  store: RecordingStore,
  sessionId: string,
  metadata: Record<string, unknown>,
  text: string,
): Promise<void> {
  await store.startSession(sessionId, metadata);
  await store.appendEvents(sessionId, [{ ts: 1, event: "read", data: { text } }]);
  await store.endSession(sessionId);
}

async function texts(store: RecordingStore, sessionId: string): Promise<unknown[]> {
  const entries = await store.getEntries(sessionId, { limit: 500 });
  return entries.filter((e) => e.event === "read").map((e) => (e.data as { text: unknown }).text);
}

const kinds: Array<[string, () => RecordingStore]> = [
  ["file", () => new LocalFileRecordingStore(dir)],
  ["memory", () => new InMemoryRecordingStore()],
];

describe.each(kinds)("%s store", (_name, make) => {
  it("serves a new instance under a reused id none of the old entries", async () => {
    const store = make();
    await recordOne(store, "pam-u-capture-7", { instance: "first" }, "alice-secret");
    await recordOne(store, "pam-u-capture-7", { instance: "second" }, "bob-output");
    expect(await texts(store, "pam-u-capture-7")).toEqual(["bob-output"]);
  });

  it("begins afresh on a start that names no instance", async () => {
    const store = make();
    await recordOne(store, "s", {}, "one");
    await recordOne(store, "s", {}, "two");
    expect(await texts(store, "s")).toEqual(["two"]);
  });

  it("keeps one recording across the same instance's reconnects", async () => {
    const store = make();
    await recordOne(store, "s", { instance: "i1" }, "first-connection");
    await recordOne(store, "s", { instance: "i1" }, "second-connection");
    expect(await texts(store, "s")).toEqual(["first-connection", "second-connection"]);
  });
});

describe.each(kinds)("retiring on a %s store", (_name, make) => {
  it("leaves nothing to serve, even with a handle still open", async () => {
    const store = make();
    await store.startSession("s", { instance: "a" });
    await store.appendEvents("s", [{ ts: 1, event: "read", data: { text: "old" } }]);
    await retireRecording(store, "s");
    expect(await store.getEntries("s", { limit: 500 })).toEqual([]);
    expect((await store.recordingMeta("s")).exists).toBe(false);
    await recordOne(store, "s", { instance: "a" }, "new");
    expect(await texts(store, "s")).toEqual(["new"]);
  });
});

it("skips a store that cannot retire", async () => {
  const legacy: RecordingStore = {
    startSession: () => Promise.resolve(),
    appendEvents: () => Promise.resolve(),
    endSession: () => Promise.resolve(),
    recordingMeta: (sessionId) => Promise.resolve({ session_id: sessionId, exists: false, size_bytes: 0 }),
    getEntries: () => Promise.resolve([]),
    getPath: () => Promise.resolve(null),
  };
  await expect(retireRecording(legacy, "s")).resolves.toBeUndefined();
});

it("retires the longest session id on disk", async () => {
  const store = new LocalFileRecordingStore(dir);
  const id = "a".repeat(200);
  await recordOne(store, id, { instance: "a" }, "x");
  await retireRecording(store, id);
  expect(readdirSync(dir).filter((name) => name.startsWith(`${id}.`))).toHaveLength(1);
});

describe("file store retirement", () => {
  it("keeps the old file aside, owner-only, and serves it to nobody", async () => {
    const store = new LocalFileRecordingStore(dir);
    await recordOne(store, "s", { instance: "first" }, "old");
    await recordOne(store, "s", { instance: "second" }, "new");
    const retired = readdirSync(dir).filter((name) => name.startsWith("s.") && name !== "s.jsonl");
    expect(retired).toHaveLength(1);
    expect(readFileSync(join(dir, retired[0] as string), "utf-8")).toContain("old");
    expect(readFileSync(join(dir, "s.jsonl"), "utf-8")).not.toContain("old");
    expect(statSync(join(dir, retired[0] as string)).mode & 0o777).toBe(0o600);
  });

  it("retires an unreadable recording rather than appending to it", async () => {
    writeFileSync(join(dir, "s.jsonl"), "not json\n", { mode: 0o600 });
    const store = new LocalFileRecordingStore(dir);
    await recordOne(store, "s", { instance: "i1" }, "mine");
    expect(readFileSync(join(dir, "s.jsonl"), "utf-8")).not.toContain("not json");
  });

  it.each([
    ["not a log_start", '{"event": "read", "data": {}}\n'],
    ["not an object", "[1, 2]\n"],
    ["a log_start without object data", '{"event": "log_start", "data": "i1"}\n'],
    ["another instance's, unterminated", '{"event": "log_start", "data": {"instance": "other"}}'],
  ])("retires a recording whose first line is %s", async (_name, firstLine) => {
    writeFileSync(join(dir, "s.jsonl"), firstLine, { mode: 0o600 });
    const store = new LocalFileRecordingStore(dir);
    await recordOne(store, "s", { instance: "i1" }, "mine");
    expect(await texts(store, "s")).toEqual(["mine"]);
    const retired = readdirSync(dir).filter((name) => name.startsWith("s.") && name !== "s.jsonl");
    expect(retired.map((name) => readFileSync(join(dir, name), "utf-8"))).toEqual([firstLine]);
  });

  it("retires a symlink planted at the path without reading through it", async () => {
    const target = join(dir, "elsewhere.txt");
    writeFileSync(target, `${JSON.stringify({ ts: 0, event: "log_start", data: { instance: "i1" } })}\n`);
    symlinkSync(target, join(dir, "s.jsonl"));
    const store = new LocalFileRecordingStore(dir);
    await recordOne(store, "s", { instance: "i1" }, "mine");
    expect(lstatSync(join(dir, "s.jsonl")).isSymbolicLink()).toBe(false);
    expect(await texts(store, "s")).toEqual(["mine"]);
    expect(readFileSync(target, "utf-8").split("\n")).toHaveLength(2);
  });

  it("retires whatever cannot be read as a recording, such as a directory", async () => {
    mkdirSync(join(dir, "s.jsonl"));
    const store = new LocalFileRecordingStore(dir);
    await recordOne(store, "s", { instance: "i1" }, "mine");
    expect(statSync(join(dir, "s.jsonl")).isFile()).toBe(true);
    expect(await texts(store, "s")).toEqual(["mine"]);
  });

  it("does not reuse a handle an unended instance left open", async () => {
    const store = new LocalFileRecordingStore(dir);
    await store.startSession("s", { instance: "first" });
    await store.appendEvents("s", [{ ts: 1, event: "read", data: { text: "old" } }]);
    await recordOne(store, "s", { instance: "second" }, "new");
    expect(await texts(store, "s")).toEqual(["new"]);
  });
});

describe("a session recording", () => {
  const settings: RecordingSettings = {
    enabledByDefault: true,
    storeType: "local",
    directory: "",
    webhookUrl: null,
    maxBytes: 0,
    controlChannelMode: "exclude",
    redactSensitive: false,
    flushIntervalS: 3600,
    flushBatchSize: 100,
  };

  async function oneConnection(recording: SessionRecording, keys: string): Promise<void> {
    await recording.start(true);
    await recording.logEvent("send", { keys });
    await recording.stop();
  }

  async function sent(store: RecordingStore): Promise<unknown[]> {
    const entries = await store.getEntries("pam-suokki-capture-4242", { limit: 500, event: "send" });
    return entries.map((e) => (e.data as { keys: unknown }).keys);
  }

  it.each(kinds)("on a %s store is not inherited by a later session reusing the id", async (_name, make) => {
    const store = make();
    const first = new SessionRecording("pam-suokki-capture-4242", store, settings);
    await oneConnection(first, "alice-1");
    await oneConnection(first, "alice-2");
    expect(await sent(store)).toEqual(["alice-1", "alice-2"]);

    const second = new SessionRecording("pam-suokki-capture-4242", store, settings);
    await oneConnection(second, "bob");
    expect(await sent(store)).toEqual(["bob"]);
  });
});
