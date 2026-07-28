//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { appendFileSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  InMemoryRecordingStore,
  LocalFileRecordingStore,
  NullRecordingStore,
  normalizeLimit,
  type RecordingEvent,
  type RecordingStore,
} from "./index.ts";

interface RecordingGolden {
  events: RecordingEvent[];
  queries: Array<{
    limit: number;
    offset: number | null;
    event: string | null;
    memory: RecordingEvent[];
    file: RecordingEvent[];
  }>;
  memory_after_end: RecordingEvent[];
  file_after_end: RecordingEvent[];
  memory_meta: { session_id: string; exists: boolean };
  deterministic_meta: { session_id: string; exists: boolean; size_bytes: number };
  memory_meta_missing: { session_id: string; exists: boolean; size_bytes: number };
  file_meta_exists_keys: string[];
  file_meta_exists: { session_id: string; exists: boolean };
  file_meta_missing: { session_id: string; exists: boolean; size_bytes: number; path: string | null };
  file_path_present: boolean;
  file_path_missing: boolean;
  null_meta: { session_id: string; exists: boolean; size_bytes: number };
  null_entries: RecordingEvent[];
  null_path: null;
  limits: Array<{ input: number; normalized: number }>;
}

const golden = loadGolden<RecordingGolden>("recording_golden.json");

let workDir: string;

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "uterm-recording-"));
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
});

/** Drop the wall-clock timestamps the lifecycle events carry. */
function stripVolatile(entries: readonly RecordingEvent[]): RecordingEvent[] {
  return entries.map((entry) => {
    if (entry.event === "log_start" || entry.event === "log_stop") {
      const { ts: _ignored, ...rest } = entry;
      return rest;
    }
    return { ...entry };
  });
}

/** Run a store through the lifecycle the corpus recorded. */
async function drive(store: RecordingStore): Promise<void> {
  await store.startSession("s1", { kind: "corpus" });
  await store.appendEvents("s1", golden.events);
}

describe("normalizeLimit", () => {
  it("matches every recorded clamp", () => {
    for (const record of golden.limits) {
      expect({ input: record.input, normalized: normalizeLimit(record.input) }).toStrictEqual(record);
    }
  });

  it("treats zero as the default rather than as empty", () => {
    expect(normalizeLimit(0)).toBe(200);
  });

  it("clamps to the one-to-five-hundred range", () => {
    expect(normalizeLimit(-5)).toBe(1);
    expect(normalizeLimit(99999)).toBe(500);
  });
});

describe("InMemoryRecordingStore", () => {
  it("records an opening event when a session starts", async () => {
    const store = new InMemoryRecordingStore();
    await store.startSession("s1", { kind: "test" });
    const entries = await store.getEntries("s1");
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ event: "log_start", data: { kind: "test" }, session_id: "s1" });
  });

  it("records a closing event when a session ends", async () => {
    const store = new InMemoryRecordingStore();
    await store.startSession("s1", {});
    await store.endSession("s1");
    expect((await store.getEntries("s1")).at(-1)).toMatchObject({ event: "log_stop", session_id: "s1" });
  });

  it("accepts events for a session that was never started", async () => {
    const store = new InMemoryRecordingStore();
    await store.appendEvents("s1", [{ ts: 1, event: "read", data: {} }]);
    expect(await store.getEntries("s1")).toHaveLength(1);
  });

  it("reports an unknown session as absent", async () => {
    expect(await new InMemoryRecordingStore().recordingMeta("nosuch")).toStrictEqual(golden.memory_meta_missing);
  });

  it("has no local path", async () => {
    expect(await new InMemoryRecordingStore().getPath("s1")).toBeNull();
  });

  it("records a closing event for a session that was never started", async () => {
    // There is no session record to mark inactive, but the event still lands
    // so a reader sees a terminated stream rather than one that just stops.
    const store = new InMemoryRecordingStore();
    await store.endSession("s1");
    expect(await store.getEntries("s1")).toHaveLength(1);
  });

  it("returns nothing for a session it has never seen", async () => {
    expect(await new InMemoryRecordingStore().getEntries("nosuch")).toStrictEqual([]);
  });
});

describe("LocalFileRecordingStore", () => {
  it("writes one JSON object per line", async () => {
    const store = new LocalFileRecordingStore(workDir);
    await store.startSession("s1", {});
    await store.appendEvents("s1", [{ ts: 1, event: "read", data: {} }]);
    await store.endSession("s1");
    expect(await store.getEntries("s1")).toHaveLength(3);
  });

  it("reopens the file when events arrive after the session ended", async () => {
    const store = new LocalFileRecordingStore(workDir);
    await store.startSession("s1", {});
    await store.endSession("s1");
    await store.appendEvents("s1", [{ ts: 1, event: "read", data: {} }]);
    expect((await store.getEntries("s1")).at(-1)).toMatchObject({ event: "read" });
  });

  it("appends rather than truncating across store instances", async () => {
    const first = new LocalFileRecordingStore(workDir);
    await first.startSession("s1", {});
    await first.endSession("s1");
    const second = new LocalFileRecordingStore(workDir);
    await second.appendEvents("s1", [{ ts: 1, event: "read", data: {} }]);
    expect(await second.getEntries("s1")).toHaveLength(3);
  });

  it("skips a line that is not valid JSON", async () => {
    // A recording truncated by a crash is still worth serving.
    const store = new LocalFileRecordingStore(workDir);
    await store.appendEvents("s1", [{ ts: 1, event: "read", data: {} }]);
    appendFileSync(join(workDir, "s1.jsonl"), "{not json\n");
    await store.appendEvents("s1", [{ ts: 2, event: "read", data: {} }]);
    expect(await store.getEntries("s1")).toHaveLength(2);
  });

  it("returns nothing for a session with no file", async () => {
    expect(await new LocalFileRecordingStore(workDir).getEntries("nosuch")).toStrictEqual([]);
  });

  it("ends a session it never opened without touching the disk", async () => {
    const store = new LocalFileRecordingStore(workDir);
    await store.endSession("nosuch");
    expect(await store.getPath("nosuch")).toBeNull();
  });

  it("reports metadata for a missing session without inventing a path", async () => {
    expect(await new LocalFileRecordingStore(workDir).recordingMeta("nosuch")).toStrictEqual(golden.file_meta_missing);
  });

  it("reports the on-disk size", async () => {
    const store = new LocalFileRecordingStore(workDir);
    await store.startSession("s1", {});
    const meta = await store.recordingMeta("s1");
    expect(meta.size_bytes).toBe(statSync(join(workDir, "s1.jsonl")).size);
    expect(Object.keys(meta).sort()).toStrictEqual(golden.file_meta_exists_keys);
  });

  it("exposes a path once the file exists and not before", async () => {
    const store = new LocalFileRecordingStore(workDir);
    expect(await store.getPath("s1")).toBeNull();
    await store.startSession("s1", {});
    expect(await store.getPath("s1")).toBe(join(workDir, "s1.jsonl"));
  });

  it("creates the recording directory owner-only", async () => {
    const nested = join(workDir, "nested");
    const store = new LocalFileRecordingStore(nested);
    await store.startSession("s1", {});
    expect(statSync(nested).mode & 0o777).toBe(0o700);
    expect(statSync(join(nested, "s1.jsonl")).mode & 0o777).toBe(0o600);
  });
});

describe("NullRecordingStore", () => {
  it("discards every write and returns nothing", async () => {
    const store = new NullRecordingStore();
    await store.startSession("s1", { kind: "test" });
    await store.appendEvents("s1", golden.events);
    await store.endSession("s1");
    expect(await store.getEntries("s1")).toStrictEqual(golden.null_entries);
    expect(await store.recordingMeta("s1")).toStrictEqual(golden.null_meta);
    expect(await store.getPath("s1")).toBe(golden.null_path);
  });
});

describe("differential parity with CPython", () => {
  it("matches every recorded query on both stores", async () => {
    const memory = new InMemoryRecordingStore();
    const file = new LocalFileRecordingStore(workDir);
    await drive(memory);
    await drive(file);

    for (const record of golden.queries) {
      const options = { limit: record.limit, offset: record.offset, event: record.event };
      expect({
        query: [record.limit, record.offset, record.event],
        memory: stripVolatile(await memory.getEntries("s1", options)),
        file: stripVolatile(await file.getEntries("s1", options)),
      }).toStrictEqual({
        query: [record.limit, record.offset, record.event],
        memory: record.memory,
        file: record.file,
      });
    }
    expect(golden.queries.length).toBeGreaterThan(10);
  });

  it("matches the recorded event stream after each store is closed", async () => {
    const memory = new InMemoryRecordingStore();
    await drive(memory);
    await memory.endSession("s1");
    expect(stripVolatile(await memory.getEntries("s1", { limit: 200 }))).toStrictEqual(golden.memory_after_end);

    const file = new LocalFileRecordingStore(workDir);
    await drive(file);
    await file.endSession("s1");
    expect(stripVolatile(await file.getEntries("s1", { limit: 200 }))).toStrictEqual(golden.file_after_end);
  });

  it("matches the recorded in-memory size, measured with CPython separators", async () => {
    // Measured over the fixed events only. A store that also holds a
    // log_start carries a wall-clock timestamp whose rendered length varies
    // between runs, so its size is not a reproducible thing to assert.
    const store = new InMemoryRecordingStore();
    await store.appendEvents("s1", golden.events);
    expect(await store.recordingMeta("s1")).toStrictEqual(golden.deterministic_meta);
  });

  it("reports a started session as existing", async () => {
    const memory = new InMemoryRecordingStore();
    await drive(memory);
    const meta = await memory.recordingMeta("s1");
    expect({ session_id: meta.session_id, exists: meta.exists }).toStrictEqual(golden.memory_meta);
  });
});
