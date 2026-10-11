//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Failures the attachment has to survive, because nothing is awaiting it.
 *
 * The poll loop and the link's `onSend` both run fire-and-forget. A rejection
 * escaping either one is an unhandled rejection, and Node exits on those — so
 * a recording store that fills its disk, or a broadcast that throws, would
 * otherwise take the whole server down. The reference's run loop catches the
 * same failures, logs them, keeps the text as `_last_error` and records a
 * `runtime_error`; these check the port does all of that and keeps going.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionConnector, WorkerMessage } from "../connectors/index.ts";
import { encodeControlFrame } from "../control-channel/index.ts";
import { InMemoryRecordingStore, type RecordingEvent } from "../recording/index.ts";
import { SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import { type Logger, type LogRecord, setLogSink } from "../telemetry/index.ts";
import { SessionHub } from "./session-hub.ts";
import { recordingSettingsFrom, SessionRecording } from "./session-recording.ts";
import { attachConnector, POLL_ERROR_BACKOFF_MS, POLL_IDLE_MS } from "./worker-attach.ts";

/** A store that refuses every write once told to, as a full disk would. */
class FailingStore extends InMemoryRecordingStore {
  failing = false;
  override async appendEvents(sessionId: string, events: readonly RecordingEvent[]): Promise<void> {
    if (this.failing) {
      throw new Error("ENOSPC: no space left on device");
    }
    await super.appendEvents(sessionId, events);
  }
}

/** A logger that keeps every warning it is handed. */
function capturingLogger() {
  const warnings: Array<{ fields: Record<string, unknown>; msg: string | undefined }> = [];
  const logger: Logger = {
    trace: () => {},
    debug: () => {},
    info: () => {},
    warn: (fields, msg) => {
      warnings.push({ fields, msg });
    },
    error: () => {},
    child: () => logger,
  };
  return { logger, warnings };
}

/** A connector whose polls hand out what it is given, one batch at a time. */
class EmittingConnector implements SessionConnector {
  readonly pending: WorkerMessage[][] = [];
  async start(): Promise<void> {}
  async stop(): Promise<void> {}
  isConnected(): boolean {
    return true;
  }
  async pollMessages(): Promise<WorkerMessage[]> {
    return this.pending.shift() ?? [];
  }
  async handleInput(): Promise<WorkerMessage[]> {
    return [];
  }
  async handleControl(): Promise<WorkerMessage[]> {
    return [];
  }
  async getSnapshot(): Promise<WorkerMessage> {
    return { type: "snapshot", screen: "seed", ts: 1 };
  }
  async getAnalysis(): Promise<string> {
    return "";
  }
  async setMode(): Promise<WorkerMessage[]> {
    return [];
  }
  async clear(): Promise<WorkerMessage[]> {
    return [];
  }
}

/** An open recording over `store`, flushed on every entry. */
async function openRecording(store: InMemoryRecordingStore) {
  const settings = recordingSettingsFrom({
    ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
    flush_interval_s: 3600,
    flush_batch_size: 1,
  });
  const recording = new SessionRecording("w1", store, settings);
  await recording.start(true);
  return recording;
}

/** Whether the loop backed off once and then went back to idle polling. */
function recovered(waits: readonly number[]): boolean {
  const backoff = waits.indexOf(POLL_ERROR_BACKOFF_MS[0] as number);
  return backoff >= 0 && waits.lastIndexOf(POLL_IDLE_MS) > backoff;
}

/** A sleep that records each wait and yields a turn. */
function recordingSleep() {
  const waits: number[] = [];
  const sleep = async (ms: number) => {
    waits.push(ms);
    await new Promise((resolve) => setImmediate(resolve));
  };
  return { waits, sleep };
}

/** Rejections nobody handled while a test ran: there must be none. */
const unhandled: unknown[] = [];
const onUnhandled = (reason: unknown) => {
  unhandled.push(reason);
};

beforeEach(() => {
  unhandled.length = 0;
  process.on("unhandledRejection", onUnhandled);
});

afterEach(() => {
  process.off("unhandledRejection", onUnhandled);
});

describe("a recording store that fails under the poll loop", () => {
  it("is logged, kept as the last error, and polled past rather than crashing the process", async () => {
    const store = new FailingStore();
    const recording = await openRecording(store);
    const connector = new EmittingConnector();
    const { logger, warnings } = capturingLogger();
    const errors: string[] = [];
    const { waits, sleep } = recordingSleep();
    const attachment = await attachConnector(new SessionHub(), "w1", connector, "hijack", {
      now: () => 5,
      recording,
      sleep,
      logger,
      onError: (error) => errors.push(error),
    });

    store.failing = true;
    // A screen, because a screen is what this recording writes an entry for.
    connector.pending.push([{ type: "snapshot", screen: "out", ts: 2 }]);
    // Past the failure and back to idle polling: the loop is still alive.
    await vi.waitFor(() => expect(recovered(waits)).toBe(true));
    await attachment.detach();
    await new Promise((resolve) => setImmediate(resolve));

    expect(errors).toStrictEqual(["ENOSPC: no space left on device"]);
    // The failure, then the `runtime_error` that could not be written to the
    // same store: both logged, neither thrown.
    expect(warnings).toStrictEqual([
      {
        fields: { session_id: "w1", error: "ENOSPC: no space left on device" },
        msg: "hosted_session_runtime_failed",
      },
      {
        fields: { session_id: "w1", error: "ENOSPC: no space left on device" },
        msg: "hosted_session_recording_failed",
      },
    ]);
    expect(unhandled).toStrictEqual([]);
  });

  it("records the failure as a runtime_error when the store can still take it", async () => {
    const store = new FailingStore();
    const recording = await openRecording(store);
    const hub = new SessionHub();
    const connector = new EmittingConnector();
    const { logger, warnings } = capturingLogger();
    const { waits, sleep } = recordingSleep();
    const attachment = await attachConnector(hub, "w1", connector, "hijack", {
      now: () => 5,
      recording,
      sleep,
      logger,
    });
    hub.router.broadcast = async () => {
      throw new Error("broadcast failed");
    };

    connector.pending.push([{ type: "term", data: "out" }], [{ type: "term", data: "again" }]);
    await vi.waitFor(() => expect(recovered(waits)).toBe(true));
    await attachment.detach();

    // The poll itself succeeded each time, which starts the schedule over:
    // what it returned could not be handed on, and that backs off once.
    const backoff = waits.indexOf(POLL_ERROR_BACKOFF_MS[0] as number);
    expect(waits.slice(backoff, backoff + 2)).toStrictEqual([POLL_ERROR_BACKOFF_MS[0], POLL_ERROR_BACKOFF_MS[0]]);
    const recorded = await store.getEntries("w1", { event: "runtime_error" });
    expect(recorded.map((entry) => entry.data)).toStrictEqual([
      { error: "broadcast failed" },
      { error: "broadcast failed" },
    ]);
    expect(warnings.map((warning) => warning.msg)).toStrictEqual([
      "hosted_session_runtime_failed",
      "hosted_session_runtime_failed",
    ]);
    expect(unhandled).toStrictEqual([]);
  });

  it("logs through the reference's runtime logger when nobody hands it one", async () => {
    const records: LogRecord[] = [];
    const restore = setLogSink((record) => records.push(record));
    try {
      const connector = new EmittingConnector();
      connector.pollMessages = async () => {
        throw new Error("read failed");
      };
      const waits: number[] = [];
      // One failure is enough; the loop then waits on a sleep that never ends.
      const sleep = (ms: number) => {
        waits.push(ms);
        return new Promise<void>(() => {});
      };
      const attachment = await attachConnector(new SessionHub(), "w1", connector, "hijack", { now: () => 5, sleep });
      await vi.waitFor(() => expect(waits).toStrictEqual([POLL_ERROR_BACKOFF_MS[0]]));
      await attachment.detach();
    } finally {
      restore();
    }
    expect(records.map((record) => [record.name, record.level, record.msg, record.fields])).toStrictEqual([
      [
        "provide.uterm.server.runtime",
        "warn",
        "hosted_session_runtime_failed",
        { session_id: "w1", error: "read failed" },
      ],
    ]);
    expect(unhandled).toStrictEqual([]);
  });

  it("has nothing more to report when nothing records", async () => {
    const connector = new EmittingConnector();
    connector.pollMessages = async () => {
      throw new Error("read failed");
    };
    const { logger, warnings } = capturingLogger();
    const waits: number[] = [];
    const sleep = (ms: number) => {
      waits.push(ms);
      return new Promise<void>(() => {});
    };
    const attachment = await attachConnector(new SessionHub(), "w1", connector, "hijack", {
      now: () => 5,
      sleep,
      logger,
    });
    await vi.waitFor(() => expect(waits).toStrictEqual([POLL_ERROR_BACKOFF_MS[0]]));
    await attachment.detach();
    // The failure alone: no recording means no `runtime_error` to fail at.
    expect(warnings.map((warning) => warning.msg)).toStrictEqual(["hosted_session_runtime_failed"]);
  });
});

describe("a failure on the way back from the link", () => {
  it("is reported rather than left as an unhandled rejection", async () => {
    const store = new FailingStore();
    const recording = await openRecording(store);
    const hub = new SessionHub();
    const { logger, warnings } = capturingLogger();
    const errors: string[] = [];
    const attachment = await attachConnector(hub, "w1", new EmittingConnector(), "hijack", {
      now: () => 5,
      recording,
      sleep: () => new Promise(() => {}),
      logger,
      onError: (error) => errors.push(error),
    });
    hub.router.broadcast = async () => {
      throw new Error("broadcast failed");
    };

    // The link answers a snapshot request through `onSend`, which nothing
    // awaits: the broadcast of that answer is what fails.
    const socket = hub.registry.get("w1")?.workerWs;
    expect(socket).toBeDefined();
    await socket?.sendText(encodeControlFrame({ type: "snapshot_req" }));
    await vi.waitFor(() => expect(errors).toStrictEqual(["broadcast failed"]));
    await attachment.detach();

    expect(warnings.map((warning) => warning.fields)).toStrictEqual([{ session_id: "w1", error: "broadcast failed" }]);
    const recorded = await store.getEntries("w1", { event: "runtime_error" });
    expect(recorded.map((entry) => entry.data)).toStrictEqual([{ error: "broadcast failed" }]);
    expect(unhandled).toStrictEqual([]);
  });
});
