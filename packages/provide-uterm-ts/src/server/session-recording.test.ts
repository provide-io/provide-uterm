//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a hosted session's recording writes, entry for entry.
 *
 * The expected entries are not written here: `session_recording_golden.json`
 * is recorded by driving the reference's own `HostedSessionRuntime` through a
 * fixed script under each configuration that changes what is written, and the
 * script itself is in the corpus, so this replays exactly those steps.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { PatternDetector } from "../annotation/index.ts";
import type { WorkerMessage } from "../connectors/index.ts";
import {
  InMemoryRecordingStore,
  LocalFileRecordingStore,
  NullRecordingStore,
  type RecordingEvent,
  type RecordingStore,
} from "../recording/index.ts";
import { SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  atPasswordPrompt,
  buildRecordingStore,
  type RecordingSettings,
  recordingRedactor,
  recordingSettingsFrom,
  SessionRecording,
} from "./session-recording.ts";

type Step =
  | ["outbound", WorkerMessage]
  | ["send", string]
  | ["wire_recv", string]
  | ["control_recv", Record<string, unknown>]
  | ["event", string, Record<string, unknown>];

interface RecordingGolden {
  session_id: string;
  script: Step[];
  annotated_script: Step[];
  configs: Record<string, Record<string, unknown>>;
  recorded: Record<string, RecordingEvent[]>;
}

const golden = loadGolden<RecordingGolden>("session_recording_golden.json");

/** The defaults, with recording on and the periodic flush out of the way. */
function settings(overrides: Record<string, unknown> = {}): RecordingSettings {
  return recordingSettingsFrom({
    ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
    enabled_by_default: true,
    flush_interval_s: 3600,
    ...overrides,
  });
}

/** Drop the fresh-by-design timestamps, as the generator does. */
function strip(entries: readonly RecordingEvent[]): RecordingEvent[] {
  return entries.map((entry) => {
    const { ts: _ts, ...rest } = entry;
    return rest.event === "log_start" ? { ...rest, data: { stripped: true } } : rest;
  });
}

/** Replay one golden step. */
async function replay(recording: SessionRecording, step: Step): Promise<void> {
  switch (step[0]) {
    case "outbound":
      await recording.logOutbound(step[1]);
      return;
    case "send":
      await recording.logSend(step[1]);
      return;
    case "wire_recv":
      await recording.logWireRecv(step[1]);
      return;
    case "control_recv":
      await recording.logControlRecv(step[1]);
      return;
    default:
      await recording.logEvent(step[1], step[2]);
  }
}

describe("replaying the reference's script", () => {
  it.each(Object.keys(golden.configs))("records what the reference records in %s mode", async (name) => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording(golden.session_id, store, settings(golden.configs[name]));
    await recording.start(true);
    for (const step of golden.script) {
      await replay(recording, step);
    }
    await recording.stop();
    expect(strip(await store.getEntries(golden.session_id, { limit: 500 }))).toStrictEqual(golden.recorded[name]);
  });
});

describe("annotating what is recorded", () => {
  const KEY = "AKIA0123456789AB"; // pragma: allowlist secret

  /** An open, annotating recording over an in-memory store. */
  async function annotating(enabled = true) {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings(), { detector: new PatternDetector() });
    await recording.start(enabled);
    return { store, recording };
  }

  /** The descriptions of every annotation recorded. */
  async function descriptions(store: InMemoryRecordingStore): Promise<string[]> {
    const entries = await store.getEntries("s1", { event: "annotation" });
    return entries.map((entry) => String((entry.data as Record<string, unknown>).description));
  }

  it("annotates as the reference does on every path a rule can match", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording(golden.session_id, store, settings(), { detector: new PatternDetector() });
    await recording.start(true);
    for (const step of golden.annotated_script) {
      await replay(recording, step);
    }
    await recording.stop();
    expect(strip(await store.getEntries(golden.session_id, { limit: 500 }))).toStrictEqual(golden.recorded.annotated);
  });

  // The three below mirror the reference's tests/server/test_output_annotation.py.
  it("scans streamed output through its escape sequences", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "term", data: `\x1b[12;5H\x1b[38;2;255;176;0m${KEY}\x1b[0m` });
    await recording.flush();
    expect(await descriptions(store)).toStrictEqual(["AWS access key detected in read"]);
  });

  it("finds a match split across frames once", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "term", data: "DROP TA" });
    await recording.logOutbound({ type: "term", data: "BLE callers;" });
    await recording.flush();
    expect(await descriptions(store)).toStrictEqual(["SQL DROP statement detected: DROP TABLE"]);
  });

  it("scans nothing when nothing is recorded", async () => {
    const { store, recording } = await annotating(false);
    await recording.logOutbound({ type: "term", data: KEY });
    await recording.logOutbound({ type: "snapshot", screen: KEY });
    await recording.logSend(KEY);
    await recording.flush();
    expect(await store.getEntries("s1")).toStrictEqual([]);
  });

  it("annotates nothing without a detector, as a runtime built without one does", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings());
    await recording.start(true);
    await recording.logOutbound({ type: "term", data: KEY });
    await recording.logOutbound({ type: "snapshot", screen: KEY });
    await recording.logSend(KEY);
    await recording.flush();
    expect(await descriptions(store)).toStrictEqual([]);
  });

  it("carries a partial match and the sequence across recordings, as the reference's runtime does", async () => {
    // The reference holds its streams and `_event_seq` on the runtime object,
    // which outlives each worker connection and the recording opened for it.
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "snapshot", screen: "$ " });
    await recording.logOutbound({ type: "term", data: "DROP TA" });
    await recording.stop();
    await recording.start(true);
    await recording.logOutbound({ type: "term", data: "BLE callers;" });
    await recording.logSend("sudo");
    await recording.flush();
    const annotations = await store.getEntries("s1", { event: "annotation" });
    expect(annotations.map((entry) => entry.data)).toMatchObject([
      { description: "SQL DROP statement detected: DROP TABLE", span: { from_seq: 1, to_seq: 1 } },
      { description: "sudo command detected: sudo", span: { from_seq: 2, to_seq: 2 } },
    ]);
  });
});

describe("frames missing the fields the loggers read", () => {
  it("records them as empty rather than failing, as the reference's lookups default", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings({ control_channel_mode: "wire" }));
    await recording.start(true);
    await recording.logOutbound({});
    await recording.logOutbound({ type: "term" });
    await recording.logOutbound({ type: "snapshot" });
    await recording.stop();
    const entries = await store.getEntries("s1");
    expect(entries.map((entry) => entry.event)).toStrictEqual([
      "log_start",
      "wire_send",
      "control_send",
      "wire_send",
      "wire_send",
      "control_send",
      "read",
      "log_stop",
    ]);
    // Terminal output with no data is an empty chunk on the wire.
    expect(entries[3]?.data).toStrictEqual({ text: "", bytes_b64: "" });
    expect(entries[6]?.data).toStrictEqual({ type: "snapshot", raw: "", raw_bytes_b64: "" });
  });
});

describe("a session that does not record", () => {
  it("writes nothing at all, not even the opening entry", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings());
    await recording.start(false);
    for (const step of golden.script) {
      await replay(recording, step);
    }
    await recording.flush();
    await recording.stop();
    expect(recording.active).toBe(false);
    expect(await store.getEntries("s1")).toStrictEqual([]);
  });

  it("still notices a password prompt, so input after recording starts is masked", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings());
    await recording.logOutbound({ type: "snapshot", screen: "Password:" });
    await recording.start(true);
    await recording.logSend("hunter2");
    await recording.stop();
    const sends = await store.getEntries("s1", { event: "send" });
    expect(sends.map((entry) => entry.data)).toStrictEqual([
      { keys: "***", bytes_b64: "Kioq", masked: true, byte_count: 7 },
    ]);
  });
});

describe("the recording's lifetime", () => {
  it("opens once, however many times it is started", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings());
    await recording.start(true);
    await recording.start(true);
    expect(recording.active).toBe(true);
    await recording.stop();
    expect((await store.getEntries("s1")).map((entry) => entry.event)).toStrictEqual(["log_start", "log_stop"]);
  });

  it("closes once, and a stop with nothing open is not an error", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings());
    await recording.stop();
    await recording.start(true);
    await recording.stop();
    await recording.stop();
    expect(recording.active).toBe(false);
    expect((await store.getEntries("s1")).map((entry) => entry.event)).toStrictEqual(["log_start", "log_stop"]);
  });

  it("holds entries until a flush, at the configured batch size", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings({ flush_batch_size: 2 }));
    await recording.start(true);
    await recording.logEvent("a", {});
    expect(await store.getEntries("s1")).toHaveLength(1);
    await recording.logEvent("b", {});
    expect(await store.getEntries("s1")).toHaveLength(3);
    await recording.logEvent("c", {});
    await recording.flush();
    expect(await store.getEntries("s1")).toHaveLength(4);
    await recording.stop();
  });

  it("stops writing once the configured byte budget is spent", async () => {
    const store = new InMemoryRecordingStore();
    const recording = new SessionRecording("s1", store, settings({ max_bytes: 1 }));
    await recording.start(true);
    await recording.logEvent("dropped", {});
    await recording.stop();
    expect((await store.getEntries("s1")).map((entry) => entry.event)).toStrictEqual(["log_start", "log_stop"]);
  });
});

describe("recording settings", () => {
  it("reads every knob the reference's runtime passes its logger", () => {
    expect(recordingSettingsFrom(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>)).toStrictEqual({
      enabledByDefault: false,
      storeType: "local",
      directory: ".uterm-recordings",
      webhookUrl: null,
      maxBytes: 0,
      controlChannelMode: "exclude",
      redactSensitive: true,
      flushIntervalS: 5,
      flushBatchSize: 100,
    });
  });

  it("carries a configured webhook address", () => {
    expect(settings({ webhook_url: "https://hooks.example/r" }).webhookUrl).toBe("https://hooks.example/r");
  });
});

describe("choosing a store", () => {
  let dir: string | undefined;
  afterEach(() => {
    if (dir !== undefined) {
      rmSync(dir, { recursive: true, force: true });
      dir = undefined;
    }
  });

  it("records to JSONL files under the configured directory by default", async () => {
    dir = mkdtempSync(join(tmpdir(), "uterm-rec-"));
    const store = buildRecordingStore(settings({ directory: dir }));
    expect(store).toBeInstanceOf(LocalFileRecordingStore);
    await store.startSession("s1", {});
    expect(await store.getPath("s1")).toBe(join(dir, "s1.jsonl"));
  });

  it.each([
    ["memory", InMemoryRecordingStore],
    ["null", NullRecordingStore],
  ] as const)("builds the %s store when asked for it", (storeType, kind) => {
    expect(buildRecordingStore(settings({ store_type: storeType }))).toBeInstanceOf(kind);
  });

  it("falls back to files for a webhook store with nowhere to send, as the reference does", () => {
    expect(buildRecordingStore(settings({ store_type: "webhook" }))).toBeInstanceOf(LocalFileRecordingStore);
  });

  it("refuses a webhook store it cannot deliver to, rather than recording nowhere", () => {
    expect(() =>
      buildRecordingStore(settings({ store_type: "webhook", webhook_url: "https://hooks.example/r" })),
    ).toThrow(/webhook/);
  });
});

describe("the recording redactor", () => {
  it("is absent when redaction is off", () => {
    expect(recordingRedactor(false)).toBeUndefined();
  });

  it("is the reference's default rule set when it is on", () => {
    expect(recordingRedactor(true)?.("login password=hunter2 now")).toBe("login [PASSWORD_REDACTED] now"); // pragma: allowlist secret
  });
});

describe("recognising a password prompt", () => {
  it.each([
    ["Password:", true],
    ["password for tim: ", true],
    ["Enter passphrase for key '/k':\n", true],
    // Python's str.rstrip strips the information separators too.
    ["Password:\x1c\x1f\x85", true],
    ["Password: hunter2", false],
    ["Password:\n$ ", false],
    ["login:", false],
    ["", false],
  ])("%j is a prompt: %s", (screen, expected) => {
    expect(atPasswordPrompt(screen)).toBe(expected);
  });
});

/** A store whose writes can be counted, to show a disabled recording costs nothing. */
class CountingStore extends NullRecordingStore implements RecordingStore {
  calls = 0;
  override startSession(sessionId: string, metadata: Record<string, unknown>): Promise<void> {
    this.calls += 1;
    return super.startSession(sessionId, metadata);
  }
}

describe("a store nobody writes to", () => {
  it("is never opened for a session that does not record", async () => {
    const store = new CountingStore();
    await new SessionRecording("s1", store, settings()).start(false);
    expect(store.calls).toBe(0);
  });
});
