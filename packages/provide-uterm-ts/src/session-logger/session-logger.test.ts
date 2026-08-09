//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { InMemoryRecordingStore, type RecordingEvent, type RecordingStore } from "../recording/index.ts";
import { makeRedactor } from "../redaction/index.ts";
import { noopLogger } from "../telemetry/index.ts";
import { loadGolden, must } from "../testing/golden.ts";
import { SessionLogger, type SessionLoggerOptions } from "./index.ts";

interface SessionLoggerGolden {
  exclude_mode: RecordingEvent[];
  wire_mode: RecordingEvent[];
  redacted: RecordingEvent[];
  quota: { entries: RecordingEvent[] };
  batch: { after_one: number; after_two: number };
}

const golden = loadGolden<SessionLoggerGolden>("session_logger_golden.json");

/** Drop the fresh-by-design timestamps, as the generator did. */
function strip(entries: readonly RecordingEvent[]): RecordingEvent[] {
  return entries.map((entry) => {
    const { ts: _ignored, ...rest } = entry;
    if (rest.event === "log_start") {
      return { ...rest, data: { stripped: true } };
    }
    return rest;
  });
}

/** Run a logger through the same script the corpus recorded. */
async function driveScript(options: SessionLoggerOptions = {}): Promise<RecordingEvent[]> {
  const store = new InMemoryRecordingStore();
  const logger = new SessionLogger(store, { flushIntervalS: 3600, logger: noopLogger, ...options });
  await logger.start("s1");
  await logger.logSend("ls -la\r");
  await logger.logSendMasked(8);
  await logger.logScreen({ screen: "hello", cursor: { x: 1 } }, new Uint8Array([0x72, 0x61, 0x77, 0xff]));
  await logger.logEvent("custom", { a: 1 });
  await logger.logWire("send", "wire out");
  await logger.logWire("recv", "wire in");
  await logger.logControl("send", { type: "hello" });
  await logger.logControl("recv", { type: "hello_ack" });
  logger.setContext({ worker: "w1", n: 2 });
  await logger.logEvent("with_context", {});
  logger.clearContext();
  await logger.logEvent("without_context", {});
  await logger.flush();
  await logger.stop();
  return strip(await store.getEntries("s1", { limit: 500 }));
}

describe("SessionLogger entries", () => {
  it("attaches the session id to every entry", async () => {
    const entries = await driveScript();
    for (const entry of entries.filter((e) => e.event !== "log_start" && e.event !== "log_stop")) {
      expect(entry.session_id).toBe("s1");
    }
  });

  it("attaches context only while it is set, stringifying values", async () => {
    const entries = await driveScript();
    expect(entries.find((e) => e.event === "with_context")?.ctx).toStrictEqual({ worker: "w1", n: "2" });
    expect(entries.find((e) => e.event === "without_context")).not.toHaveProperty("ctx");
  });

  it("encodes sent keystrokes as CP437 alongside the text", async () => {
    const entries = await driveScript();
    expect(entries.find((e) => e.event === "send")?.data).toStrictEqual({
      keys: "ls -la\r",
      bytes_b64: "bHMgLWxhDQ==",
    });
  });

  it("encodes a non-ASCII keystroke as CP437, not UTF-8", async () => {
    // The ASCII case above cannot tell the encodings apart; these can.
    // Expectations recorded from CPython's encode("cp437", errors="replace").
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store, { flushIntervalS: 3600, logger: noopLogger });
    await logger.start("s1");
    await logger.logSend("░▒");
    await logger.logSend("café");
    await logger.stop();
    const sends = (await store.getEntries("s1", { limit: 500 })).filter((e) => e.event === "send");
    expect((must(sends[0], "the first send entry").data as Record<string, unknown>).bytes_b64).toBe("sLE=");
    expect((must(sends[1], "the second send entry").data as Record<string, unknown>).bytes_b64).toBe("Y2Fmgg==");
  });

  it("records a masked send without the value", async () => {
    const entries = await driveScript();
    const masked = entries.filter((e) => e.event === "send")[1]?.data as Record<string, unknown>;
    expect(masked).toMatchObject({ keys: "***", masked: true, byte_count: 8 });
  });

  it("round-trips a high byte through the CP437 raw capture", async () => {
    const entries = await driveScript();
    const read = entries.find((e) => e.event === "read")?.data as Record<string, unknown>;
    // 0xff decodes to a non-breaking space and must re-encode to 0xff.
    expect(read.raw_bytes_b64).toBe("cmF3/w==");
  });
});

describe("SessionLogger control-channel mode", () => {
  it("omits wire and control entries by default", async () => {
    const events = (await driveScript()).map((e) => e.event);
    expect(events).not.toContain("wire_send");
    expect(events).not.toContain("control_recv");
  });

  it("records them in wire mode", async () => {
    const events = (await driveScript({ controlChannelMode: "wire" })).map((e) => e.event);
    expect(events).toContain("wire_send");
    expect(events).toContain("wire_recv");
    expect(events).toContain("control_send");
    expect(events).toContain("control_recv");
  });

  it("encodes a wire chunk as UTF-8, unlike a keystroke", async () => {
    const entries = await driveScript({ controlChannelMode: "wire" });
    const wire = entries.find((e) => e.event === "wire_send")?.data as Record<string, unknown>;
    expect(wire.bytes_b64).toBe(Buffer.from("wire out", "utf-8").toString("base64"));
  });
});

describe("SessionLogger redaction", () => {
  it("redacts keystrokes, raw output and nested snapshot strings", async () => {
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store, {
      flushIntervalS: 3600,
      logger: noopLogger,
      redactor: makeRedactor(["secret\\w*"]),
    });
    await logger.start("s1");
    await logger.logSend("secret123");
    await logger.logScreen({ screen: "a secretword b", nested: { deep: ["secretx"] } }, new Uint8Array());
    await logger.stop();
    const entries = await store.getEntries("s1", { limit: 500 });
    expect(
      (
        must(
          entries.find((e) => e.event === "send"),
          "a redacted send entry",
        ).data as Record<string, unknown>
      ).keys,
    ).toBe("[REDACTED]");
    const read = entries.find((e) => e.event === "read")?.data as Record<string, unknown>;
    expect(read.screen).toBe("a [REDACTED] b");
    expect(read.nested).toStrictEqual({ deep: ["[REDACTED]"] });
  });

  it("leaves non-string values alone", async () => {
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store, {
      flushIntervalS: 3600,
      logger: noopLogger,
      redactor: makeRedactor(["x"]),
    });
    await logger.start("s1");
    await logger.logScreen({ n: 1, flag: true, nothing: null }, new Uint8Array());
    await logger.stop();
    const read = (await store.getEntries("s1", { limit: 500 })).find((e) => e.event === "read")?.data as Record<
      string,
      unknown
    >;
    expect({ n: read.n, flag: read.flag, nothing: read.nothing }).toStrictEqual({ n: 1, flag: true, nothing: null });
  });
});

describe("SessionLogger buffering", () => {
  it("flushes once the batch is full without waiting for the interval", async () => {
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store, { batchSize: 2, flushIntervalS: 3600, logger: noopLogger });
    await logger.start("s1");
    await logger.logEvent("a", {});
    const afterOne = (await store.getEntries("s1", { limit: 500 })).length;
    await logger.logEvent("b", {});
    const afterTwo = (await store.getEntries("s1", { limit: 500 })).length;
    await logger.stop();
    expect({ after_one: afterOne, after_two: afterTwo }).toStrictEqual(golden.batch);
  });

  it("keeps the batch buffered when the store rejects it", async () => {
    // Losing the batch on a transient store failure would silently drop
    // session history, so it must survive for the next attempt.
    let failing = true;
    const accepted: RecordingEvent[] = [];
    const store: RecordingStore = {
      startSession: () => Promise.resolve(),
      appendEvents: (_id, events) => {
        if (failing) {
          return Promise.reject(new Error("store down"));
        }
        accepted.push(...events);
        return Promise.resolve();
      },
      endSession: () => Promise.resolve(),
      recordingMeta: (id) => Promise.resolve({ session_id: id, exists: true, size_bytes: 0 }),
      getEntries: () => Promise.resolve([]),
      getPath: () => Promise.resolve(null),
    };

    const logger = new SessionLogger(store, { flushIntervalS: 3600, logger: noopLogger });
    await logger.start("s1");
    await logger.logEvent("a", {});
    await expect(logger.flush()).rejects.toThrow("store down");
    failing = false;
    await logger.flush();
    expect(accepted.map((e) => e.event)).toStrictEqual(["a"]);
  });

  it("retries on the next tick after a periodic flush fails, without dying", async () => {
    // A transient store outage must not kill the flusher: the batch stays
    // buffered and the next tick retries it. If the failure escaped, the
    // timer would be torn down and the session would stop recording.
    let failing = true;
    const accepted: RecordingEvent[] = [];
    const warnings: string[] = [];
    const store: RecordingStore = {
      startSession: () => Promise.resolve(),
      appendEvents: (_id, events) => {
        if (failing) {
          return Promise.reject(new Error("store down"));
        }
        accepted.push(...events);
        return Promise.resolve();
      },
      endSession: () => Promise.resolve(),
      recordingMeta: (id) => Promise.resolve({ session_id: id, exists: true, size_bytes: 0 }),
      getEntries: () => Promise.resolve([]),
      getPath: () => Promise.resolve(null),
    };

    const logger = new SessionLogger(store, {
      flushIntervalS: 0.001,
      logger: { ...noopLogger, warn: (fields) => warnings.push(String(fields.event)) },
    });
    await logger.start("s1");
    await logger.logEvent("a", {});
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(warnings).toContain("session_logger_periodic_flush_failed");
    failing = false;
    await new Promise((resolve) => setTimeout(resolve, 20));
    await logger.stop();
    expect(accepted.map((e) => e.event)).toStrictEqual(["a"]);
  });

  it("flushes remaining entries on stop", async () => {
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store, { flushIntervalS: 3600, logger: noopLogger });
    await logger.start("s1");
    await logger.logEvent("a", {});
    await logger.stop();
    expect((await store.getEntries("s1", { limit: 500 })).map((e) => e.event)).toContain("a");
  });
});

describe("SessionLogger defaults", () => {
  it("uses the documented flush interval and its own logger when none are given", async () => {
    // Constructed with no options at all, so the defaulted interval and
    // logger are exercised rather than the ones every other test injects.
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store);
    await logger.start("s1");
    await logger.logEvent("a", {});
    await logger.stop();
    expect((await store.getEntries("s1", { limit: 500 })).map((e) => e.event)).toContain("a");
  });

  it("starts the byte count at zero when the store reports an unusable size", async () => {
    // A remote store that cannot measure itself must not poison the quota
    // arithmetic with NaN, which would compare false against every budget
    // and silently disable the limit.
    const store = new InMemoryRecordingStore();
    const unmeasurable: RecordingStore = {
      ...store,
      startSession: (id, meta) => store.startSession(id, meta),
      appendEvents: (id, events) => store.appendEvents(id, events),
      endSession: (id) => store.endSession(id),
      getEntries: (id, options) => store.getEntries(id, options),
      getPath: (id) => store.getPath(id),
      recordingMeta: (id) =>
        Promise.resolve({ session_id: id, exists: true, size_bytes: "not a number" as unknown as number }),
    };
    const logger = new SessionLogger(unmeasurable, { maxBytes: 500, flushIntervalS: 3600, logger: noopLogger });
    await logger.start("s1");
    await logger.logEvent("a", {});
    await logger.stop();
    expect((await store.getEntries("s1", { limit: 500 })).map((e) => e.event)).toContain("a");
  });

  it("stops cleanly when it was never started", async () => {
    // No session id, so there is nothing to end and no entry to attach one
    // to; this must not throw on a logger a caller abandoned.
    const logger = new SessionLogger(new InMemoryRecordingStore(), { logger: noopLogger });
    await logger.logEvent("orphan", {});
    await expect(logger.stop()).resolves.toBeUndefined();
  });
});

describe("SessionLogger quota", () => {
  it("suppresses writes past the byte budget and warns once", async () => {
    const warnings: string[] = [];
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store, {
      maxBytes: 1,
      flushIntervalS: 3600,
      logger: { ...noopLogger, warn: (fields) => warnings.push(String(fields.event)) },
    });
    await logger.start("s1");
    await logger.logEvent("first", { a: 1 });
    await logger.logEvent("second", { a: 2 });
    await logger.stop();
    expect(strip(await store.getEntries("s1", { limit: 500 }))).toStrictEqual(golden.quota.entries);
    expect(warnings).toStrictEqual(["session_logger_quota_reached"]);
  });

  it("writes without limit when no budget is set", async () => {
    const store = new InMemoryRecordingStore();
    const logger = new SessionLogger(store, { flushIntervalS: 3600, logger: noopLogger });
    await logger.start("s1");
    await logger.logEvent("a", {});
    await logger.stop();
    expect((await store.getEntries("s1", { limit: 500 })).map((e) => e.event)).toContain("a");
  });
});

describe("differential parity with CPython", () => {
  it("matches the recorded entries with control frames excluded", async () => {
    expect(await driveScript()).toStrictEqual(golden.exclude_mode);
  });

  it("matches the recorded entries in wire mode", async () => {
    expect(await driveScript({ controlChannelMode: "wire" })).toStrictEqual(golden.wire_mode);
  });

  it("matches the recorded entries with a redactor configured", async () => {
    expect(await driveScript({ controlChannelMode: "wire", redactor: makeRedactor(["secret\\w*"]) })).toStrictEqual(
      golden.redacted,
    );
  });
});
