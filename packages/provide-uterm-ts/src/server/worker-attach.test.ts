//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Joining a connector to the hub, and what travels each way once it is joined.
 *
 * The point of these is the round trip. The hub does not call the connector: it
 * writes an encoded frame to a socket, and everything after that — the decoder,
 * the worker link's reading of the frame, the connector's answer, and the
 * hub's handling of what comes back — has to line up, or a lease would be
 * granted over something that never pauses.
 */

import { describe, expect, it, vi } from "vitest";
import { PatternDetector } from "../annotation/index.ts";
import type { SessionConnector, WorkerMessage } from "../connectors/index.ts";
import { ShellSessionConnector } from "../connectors/index.ts";
import { ControlFrameDecoder, encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import { InMemoryRecordingStore, type RecordingEvent } from "../recording/index.ts";
import { SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import { SessionHub } from "./session-hub.ts";
import { recordingSettingsFrom, SessionRecording } from "./session-recording.ts";
import { attachConnector, POLL_ERROR_BACKOFF_MS, POLL_IDLE_MS, workerSnapshotFrame } from "./worker-attach.ts";

/** The socket the hub is holding for a worker, for a test that writes to it. */
function socketOf(hub: SessionHub, workerId: string) {
  const socket = hub.registry.get(workerId)?.workerWs;
  if (socket === undefined) {
    throw new Error("nothing attached");
  }
  return socket;
}

/** A browser that records decoded control frames from the real broadcast path. */
class RecordingBrowser {
  readonly sent: Record<string, unknown>[] = [];
  readonly #decoder = new ControlFrameDecoder();

  async sendText(payload: string): Promise<void> {
    for (const chunk of this.#decoder.feed(payload)) {
      if (chunk.kind === "control") {
        this.sent.push(chunk.control);
      }
    }
  }
}

/** A connector that records what it was asked and answers with what it is told. */
class RecordingConnector implements SessionConnector {
  readonly seen: string[] = [];
  readonly answer: WorkerMessage[];
  constructor(answer: WorkerMessage[] = []) {
    this.answer = answer;
  }
  async start(): Promise<void> {}
  async stop(): Promise<void> {}
  isConnected(): boolean {
    return true;
  }
  async pollMessages(): Promise<WorkerMessage[]> {
    return [];
  }
  async handleInput(data: string): Promise<WorkerMessage[]> {
    this.seen.push(`input:${data}`);
    return this.answer;
  }
  async handleControl(action: string): Promise<WorkerMessage[]> {
    this.seen.push(`control:${action}`);
    return this.answer;
  }
  async getSnapshot(): Promise<WorkerMessage> {
    return { type: "snapshot", screen: "seed", ts: 1 };
  }
  async getAnalysis(): Promise<string> {
    return "";
  }
  async setMode(mode: string): Promise<WorkerMessage[]> {
    this.seen.push(`mode:${mode}`);
    return [];
  }
  async clear(): Promise<WorkerMessage[]> {
    return [];
  }
}

class PausedSnapshotConnector extends RecordingConnector {
  readonly entered: Promise<void>;
  readonly #markEntered: () => void;
  readonly #resume: Promise<void>;
  #release: () => void;

  constructor() {
    super();
    let markEntered = () => {};
    let release = () => {};
    this.entered = new Promise<void>((resolve) => {
      markEntered = resolve;
    });
    this.#resume = new Promise<void>((resolve) => {
      release = resolve;
    });
    this.#markEntered = markEntered;
    this.#release = release;
  }

  resume(): void {
    this.#release();
  }

  override async getSnapshot(): Promise<WorkerMessage> {
    this.#markEntered();
    await this.#resume;
    return { type: "snapshot", screen: "worker-a", ts: 1 };
  }
}

class FixedSnapshotConnector extends RecordingConnector {
  readonly #screen: string;

  constructor(screen: string) {
    super();
    this.#screen = screen;
  }

  override async getSnapshot(): Promise<WorkerMessage> {
    return { type: "snapshot", screen: this.#screen, ts: 1 };
  }
}

describe("building the frame a worker's snapshot becomes", () => {
  it("fills in every field a connector left out, as the reference's builder does", () => {
    // `raw_tail` is the tell: no connector sets it, and it is on the wire for
    // every snapshot because the frame is built here rather than there.
    expect(workerSnapshotFrame({}, 42)).toEqual({
      type: "snapshot",
      screen: "",
      cursor: { x: 0, y: 0 },
      cols: 80,
      rows: 25,
      screen_hash: "",
      cursor_at_end: true,
      has_trailing_space: false,
      prompt_detected: null,
      raw_tail: null,
      // Null, not 0: a connector that reports no ingest counters is saying
      // "this build cannot tell you", which must stay distinguishable from a
      // worker reporting a genuine zero bytes read.
      chunks_read: null,
      bytes_read: null,
      ts: 42,
    });
  });

  it.each([["3"], [1.5], [true]])("drops a count that is not an integer, %j, rather than guessing", (value) => {
    expect(workerSnapshotFrame({ chunks_read: value, bytes_read: value }, 0)).toMatchObject({
      chunks_read: null,
      bytes_read: null,
    });
  });

  it("refuses a size that would render as nothing, rather than passing it on", () => {
    const frame = workerSnapshotFrame({ cols: 0, rows: "many", ts: "soon" }, 7);
    expect(frame).toMatchObject({ cols: 80, rows: 25, ts: 7 });
  });

  it("keeps what a connector did set", () => {
    const frame = workerSnapshotFrame(
      {
        screen: "hi",
        cursor: { x: 2, y: 3 },
        cols: 100,
        rows: 40,
        screen_hash: "abc",
        cursor_at_end: false,
        has_trailing_space: true,
        prompt_detected: { prompt_id: "p" },
        raw_tail: "t",
        chunks_read: 3,
        bytes_read: 4096,
        ts: 9,
      },
      0,
    );
    expect(frame).toEqual({
      type: "snapshot",
      screen: "hi",
      cursor: { x: 2, y: 3 },
      cols: 100,
      rows: 40,
      screen_hash: "abc",
      cursor_at_end: false,
      has_trailing_space: true,
      prompt_detected: { prompt_id: "p" },
      raw_tail: "t",
      // Forwarded from the worker, not recomputed here: only the worker's own
      // reader loop can count what it ingested before any emulator work.
      chunks_read: 3,
      bytes_read: 4096,
      ts: 9,
    });
  });
});

describe("attaching", () => {
  it("gives the hub a worker to lease, in the mode the session was configured in", async () => {
    const hub = new SessionHub();
    await attachConnector(hub, "w1", new ShellSessionConnector("w1", "W1"), "open");
    expect(hub.registry.get("w1")?.workerWs).toBeDefined();
    expect(hub.registry.get("w1")?.inputMode).toBe("open");
  });

  it("seeds the screen the connector already had, so a read before any input answers", async () => {
    const hub = new SessionHub();
    await attachConnector(hub, "w1", new ShellSessionConnector("w1", "W1"), "hijack");
    const snapshot = await hub.getLastSnapshot("w1");
    expect(snapshot).toMatchObject({ type: "snapshot", cols: 80, rows: 25 });
    expect(String(snapshot?.screen)).toContain("W1");
  });

  it("reads its own clock when nobody hands it one", async () => {
    const hub = new SessionHub();
    const before = Date.now() / 1000;
    await attachConnector(hub, "w1", new RecordingConnector(), "hijack");
    // The connector's own `ts` of 1 is kept — `safeFloat` only falls back for
    // a value it cannot read — so the clock shows up on a message without one.
    await socketOf(hub, "w1").sendText(encodeControlFrame({ type: "snapshot_req" }));
    const ts = Number((await hub.getLastSnapshot("w1"))?.ts);
    // Seconds, as every instant on this wire is — not milliseconds.
    expect(ts).toBeGreaterThanOrEqual(before);
    expect(ts).toBeLessThan(before + 60);
  });

  it("records a screen carrying no detected prompt without inventing one", async () => {
    const hub = new SessionHub();
    await attachConnector(hub, "w1", new RecordingConnector(), "hijack", { now: () => 5 });
    const events = hub.registry.get("w1")?.events.toArray() ?? [];
    expect(events[0]).toMatchObject({ type: "snapshot", data: { prompt_id: null, screen: "seed" } });
  });

  it("broadcasts the committed snapshot carrying the current and ring sequence", async () => {
    const hub = new SessionHub();
    await attachConnector(hub, "w1", new RecordingConnector(), "hijack", { now: () => 5 });
    const browser = new RecordingBrowser();
    await hub.connections.registerBrowser("w1", browser, "viewer", { deferBroadcast: true });
    hub.connections.activateBrowserBroadcasts("w1", browser);

    await socketOf(hub, "w1").sendText(encodeControlFrame({ type: "snapshot_req" }));

    const state = hub.registry.get("w1");
    const current = state?.lastSnapshot;
    const event = state?.events.at(-1);
    const broadcast = browser.sent.at(-1);
    expect(current).toMatchObject({ type: "snapshot", screen: "seed", event_seq: 2 });
    expect(event).toMatchObject({
      seq: 2,
      data: { screen: "seed", event_seq: 2 },
    });
    expect(broadcast).toEqual(current);
    expect(broadcast).not.toBe(current);
  });

  it("fences a paused snapshot when a replacement worker commits first", async () => {
    const hub = new SessionHub();
    const workerA = new PausedSnapshotConnector();
    const attachA = attachConnector(hub, "w1", workerA, "hijack", { now: () => 1 });
    await workerA.entered;

    const browser = new RecordingBrowser();
    await hub.connections.registerBrowser("w1", browser, "viewer", { deferBroadcast: true });
    hub.connections.activateBrowserBroadcasts("w1", browser);

    await attachConnector(hub, "w1", new RecordingConnector(), "hijack", { now: () => 2 });
    workerA.resume();
    await attachA;

    const state = hub.registry.get("w1");
    expect(state?.lastSnapshot).toMatchObject({ screen: "seed", event_seq: 1 });
    expect(state?.events.toArray()).toEqual([
      expect.objectContaining({ seq: 1, data: expect.objectContaining({ screen: "seed", event_seq: 1 }) }),
    ]);
    expect(browser.sent).toHaveLength(1);
    expect(browser.sent[0]).toMatchObject({ screen: "seed", event_seq: 1 });
  });

  it("fences a replaced worker paused after snapshot commit but before broadcast", async () => {
    const hub = new SessionHub();
    let markEntered = () => {};
    let release = () => {};
    const entered = new Promise<void>((resolve) => {
      markEntered = resolve;
    });
    const resume = new Promise<void>((resolve) => {
      release = resolve;
    });
    const originalBroadcast = hub.router.broadcast.bind(hub.router);
    hub.router.broadcast = async (workerId, message, expectedWorker, expectedEventSeq) => {
      if (message.screen === "worker-a") {
        markEntered();
        await resume;
      }
      await originalBroadcast(workerId, message, expectedWorker, expectedEventSeq);
    };

    const attachA = attachConnector(hub, "w1", new FixedSnapshotConnector("worker-a"), "hijack", {
      now: () => 1,
    });
    await entered;

    const browser = new RecordingBrowser();
    await hub.connections.registerBrowser("w1", browser, "viewer", { deferBroadcast: true });
    hub.connections.activateBrowserBroadcasts("w1", browser);

    await attachConnector(hub, "w1", new FixedSnapshotConnector("worker-b"), "hijack", { now: () => 2 });
    release();
    await attachA;

    const state = hub.registry.get("w1");
    expect(state?.workerWs).toBeDefined();
    expect(state?.lastSnapshot).toMatchObject({ screen: "worker-b", event_seq: 2 });
    expect(state?.events.toArray()).toEqual([
      expect.objectContaining({ seq: 1, data: expect.objectContaining({ screen: "worker-a", event_seq: 1 }) }),
      expect.objectContaining({ seq: 2, data: expect.objectContaining({ screen: "worker-b", event_seq: 2 }) }),
    ]);
    expect(browser.sent).toEqual([expect.objectContaining({ screen: "worker-b", event_seq: 2 })]);
  });

  it("broadcasts a paused snapshot after an unrelated event advances the ring", async () => {
    const hub = new SessionHub();
    let markEntered = () => {};
    let release = () => {};
    const entered = new Promise<void>((resolve) => {
      markEntered = resolve;
    });
    const resume = new Promise<void>((resolve) => {
      release = resolve;
    });
    const originalBroadcast = hub.router.broadcast.bind(hub.router);
    hub.router.broadcast = async (workerId, message, expectedWorker, expectedEventSeq) => {
      markEntered();
      await resume;
      await originalBroadcast(workerId, message, expectedWorker, expectedEventSeq);
    };

    const attach = attachConnector(hub, "w1", new FixedSnapshotConnector("current"), "hijack", { now: () => 1 });
    await entered;
    const browser = new RecordingBrowser();
    await hub.connections.registerBrowser("w1", browser, "viewer", { deferBroadcast: true });
    hub.connections.activateBrowserBroadcasts("w1", browser);

    const unrelated = await hub.appendEvent("w1", "hijack_heartbeat", { owner: "operator" });
    release();
    await attach;

    const state = hub.registry.get("w1");
    expect(unrelated.seq).toBe(2);
    expect(state?.eventSeq).toBe(2);
    expect(state?.lastSnapshot).toMatchObject({ screen: "current", event_seq: 1 });
    expect(browser.sent).toEqual([expect.objectContaining({ screen: "current", event_seq: 1 })]);
  });
});

describe("what the hub sends, and what the worker does with it", () => {
  it("pauses the connector when a lease is taken, and resumes it when it is given back", async () => {
    const hub = new SessionHub();
    const connector = new RecordingConnector();
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5 });
    const socket = socketOf(hub, "w1");

    await socket.sendText(encodeControlFrame({ type: "control", action: "pause" }));
    await socket.sendText(encodeControlFrame({ type: "control", action: "resume" }));
    await socket.sendText(encodeControlFrame({ type: "control", action: "step" }));

    expect(connector.seen).toEqual(["control:pause", "control:resume", "control:step"]);
  });

  it("types raw terminal bytes into the session rather than at the control channel", async () => {
    const hub = new SessionHub();
    const connector = new RecordingConnector();
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5 });

    await socketOf(hub, "w1").sendText(encodeTerminalData("hello\r"));

    expect(connector.seen).toEqual(["input:hello\r"]);
  });

  it("takes a resize without a terminal to resize, rather than failing on it", async () => {
    const hub = new SessionHub();
    const connector = new RecordingConnector();
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5 });

    await socketOf(hub, "w1").sendText(encodeControlFrame({ type: "resize", cols: 120, rows: 40 }));

    expect(connector.seen).toEqual([]);
  });

  it("answers a snapshot request with a screen stamped now, which is what a poll waits for", async () => {
    const hub = new SessionHub();
    const connector = new ShellSessionConnector("w1", "W1");
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5000 });
    // The seeded screen carries the connector's own timestamp; the answer to a
    // request carries the link's, which is what makes it *fresh*.
    await socketOf(hub, "w1").sendText(encodeControlFrame({ type: "snapshot_req" }));
    expect((await hub.getLastSnapshot("w1"))?.ts).toBe(5000);
  });

  it("passes a message that is not a screen on without recording it as one", async () => {
    const hub = new SessionHub();
    const connector = new RecordingConnector([{ type: "analysis", formatted: "idle" }]);
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5 });
    const seeded = await hub.getLastSnapshot("w1");

    await socketOf(hub, "w1").sendText(encodeTerminalData("x"));

    expect(await hub.getLastSnapshot("w1")).toBe(seeded);
  });

  it("keeps the session alive when a control action it applies throws", async () => {
    // The link swallows what the worker throws: a session can die between a
    // frame arriving and being applied, and that must not take the link down.
    const hub = new SessionHub();
    const connector = new RecordingConnector();
    connector.handleControl = async () => {
      throw new Error("gone");
    };
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5 });
    await expect(
      socketOf(hub, "w1").sendText(encodeControlFrame({ type: "control", action: "pause" })),
    ).resolves.toBeUndefined();
  });
});

describe("detaching", () => {
  it("takes the worker off the hub, and forgets it when nothing else holds it", async () => {
    const hub = new SessionHub();
    const attachment = await attachConnector(hub, "w1", new RecordingConnector(), "hijack", { now: () => 5 });

    await attachment.detach();

    expect(hub.registry.contains("w1")).toBe(false);
  });
});

/** An open recording over an in-memory store, flushed on every entry. */
async function openRecording(overrides: Record<string, unknown> = {}) {
  const store = new InMemoryRecordingStore();
  const settings = recordingSettingsFrom({
    ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
    flush_interval_s: 3600,
    flush_batch_size: 1,
    ...overrides,
  });
  const recording = new SessionRecording("w1", store, settings);
  await recording.start(true);
  return { store, recording };
}

/** Each entry as `event` plus the one field that tells entries apart. */
function summarise(entries: readonly RecordingEvent[]): string[] {
  return entries.map((entry) => {
    const data = entry.data as Record<string, unknown>;
    if (entry.event === "read") {
      return `read:${String(data.screen)}`;
    }
    if (entry.event === "send") {
      return `send:${String(data.keys)}`;
    }
    return String(entry.event);
  });
}

describe("recording what crosses the attachment", () => {
  it("opens with the runtime's start event, then the screen the worker seeded", async () => {
    const hub = new SessionHub();
    const { store, recording } = await openRecording();
    await attachConnector(hub, "w1", new RecordingConnector(), "hijack", { now: () => 5, recording });
    const entries = await store.getEntries("w1");
    expect(summarise(entries)).toStrictEqual(["log_start", "runtime_started", "read:seed"]);
    expect(entries[1]?.data).toStrictEqual({ session_id: "w1" });
  });

  it("records typed input, and the screen the connector answers it with", async () => {
    const hub = new SessionHub();
    const { store, recording } = await openRecording();
    const connector = new RecordingConnector([{ type: "snapshot", screen: "$ ls", ts: 2 }]);
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5, recording });

    await socketOf(hub, "w1").sendText(encodeTerminalData("ls\r"));

    expect(summarise(await store.getEntries("w1")).slice(3)).toStrictEqual(["send:ls\r", "read:$ ls"]);
  });

  it("masks what is typed at a password prompt the worker put on screen", async () => {
    const hub = new SessionHub();
    const { store, recording } = await openRecording();
    const connector = new RecordingConnector([{ type: "snapshot", screen: "Password: ", ts: 2 }]);
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5, recording });
    const socket = socketOf(hub, "w1");

    await socket.sendText(encodeTerminalData("su\r"));
    await socket.sendText(encodeTerminalData("hunter2\r"));

    const sends = await store.getEntries("w1", { event: "send" });
    expect(sends.map((entry) => entry.data)).toStrictEqual([
      { keys: "su\r", bytes_b64: "c3UN" },
      { keys: "***", bytes_b64: "Kioq", masked: true, byte_count: 8 },
    ]);
  });

  it("records the frames both ways in wire mode, as the reference's runtime does", async () => {
    const hub = new SessionHub();
    const { store, recording } = await openRecording({ control_channel_mode: "wire" });
    const connector = new RecordingConnector([{ type: "term", data: "out" }]);
    await attachConnector(hub, "w1", connector, "hijack", { now: () => 5, recording });
    const socket = socketOf(hub, "w1");

    await socket.sendText(encodeControlFrame({ type: "control", action: "pause" }));
    // The link's acknowledgement goes out on its own turn, as a socket write
    // would; let it land so the two exchanges do not interleave.
    await new Promise((resolve) => setImmediate(resolve));
    await socket.sendText(encodeTerminalData("x"));

    const events = summarise(await store.getEntries("w1"));
    expect(events.slice(5)).toStrictEqual([
      // The pause: what arrived, what it decoded to, what the connector said,
      // and the link's acknowledgement, which is a control frame.
      "wire_recv",
      "control_recv",
      "wire_send",
      "wire_send",
      "control_send",
      // The keystroke: what arrived, the input itself, and the output it made.
      "wire_recv",
      "send:x",
      "wire_send",
    ]);
  });

  it("writes nothing when the recording it was handed is not open", async () => {
    const hub = new SessionHub();
    const store = new InMemoryRecordingStore();
    const settings = recordingSettingsFrom(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>);
    const recording = new SessionRecording("w1", store, settings);
    await attachConnector(hub, "w1", new RecordingConnector(), "hijack", { now: () => 5, recording });
    await socketOf(hub, "w1").sendText(encodeTerminalData("x"));
    expect(await store.getEntries("w1")).toStrictEqual([]);
  });
});

/** A connector that produces output on its own, handed out one poll at a time. */
class EmittingConnector extends RecordingConnector {
  readonly pending: WorkerMessage[][] = [];
  polls = 0;
  override async pollMessages(): Promise<WorkerMessage[]> {
    this.polls += 1;
    return this.pending.shift() ?? [];
  }
}

/** A browser that keeps the terminal output it is sent, not only control frames. */
class OutputBrowser {
  readonly output: string[] = [];
  readonly #decoder = new ControlFrameDecoder();

  async sendText(payload: string): Promise<void> {
    for (const chunk of this.#decoder.feed(payload)) {
      if (chunk.kind === "data") {
        this.output.push(chunk.data);
      }
    }
  }
}

/** An open recording, flushed on every entry, with or without a detector. */
async function pollRecording(detector?: PatternDetector) {
  const store = new InMemoryRecordingStore();
  const settings = recordingSettingsFrom({
    ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
    flush_interval_s: 3600,
    flush_batch_size: 1,
  });
  const recording = new SessionRecording("w1", store, settings, { detector });
  await recording.start(true);
  return { store, recording };
}

/** A sleep that records each wait, yields a turn, and says when it has seen enough. */
function countingSleep(enough: (waits: readonly number[]) => boolean) {
  const waits: number[] = [];
  let release: () => void = () => {};
  const done = new Promise<void>((resolve) => {
    release = resolve;
  });
  const sleep = async (ms: number) => {
    waits.push(ms);
    if (enough(waits)) {
      release();
    }
    await new Promise((resolve) => setImmediate(resolve));
  };
  return { waits, done, sleep };
}

describe("output a connector produces on its own", () => {
  it("is polled for, broadcast to the hub, and recorded and annotated like any other", async () => {
    const hub = new SessionHub();
    const { store, recording } = await pollRecording(new PatternDetector());
    const connector = new EmittingConnector();
    const attachment = await attachConnector(hub, "w1", connector, "hijack", { now: () => 5, recording });
    const browser = new OutputBrowser();
    await hub.connections.registerBrowser("w1", browser, "viewer", { deferBroadcast: true });
    hub.connections.activateBrowserBroadcasts("w1", browser);

    connector.pending.push([
      { type: "term", data: "DROP TABLE users;" },
      { type: "snapshot", screen: "polled", ts: 6 },
    ]);

    await vi.waitFor(async () => {
      expect((await hub.getLastSnapshot("w1"))?.screen).toBe("polled");
    });
    await attachment.detach();
    expect(browser.output).toContain("DROP TABLE users;");
    expect(summarise(await store.getEntries("w1"))).toStrictEqual([
      "log_start",
      "runtime_started",
      "read:seed",
      "annotation",
      "read:polled",
    ]);
    const [annotation] = await store.getEntries("w1", { event: "annotation" });
    expect(annotation?.data).toMatchObject({ description: "SQL DROP statement detected: DROP TABLE" });
  });

  it("waits the reference's 50 ms between polls that found nothing", async () => {
    const { waits, done, sleep } = countingSleep((seen) => seen.length === 3);
    const attachment = await attachConnector(new SessionHub(), "w1", new EmittingConnector(), "hijack", {
      now: () => 5,
      sleep,
    });
    await done;
    await attachment.detach();
    expect(POLL_IDLE_MS).toBe(50);
    expect(waits.slice(0, 3)).toStrictEqual([POLL_IDLE_MS, POLL_IDLE_MS, POLL_IDLE_MS]);
  });

  it("polls again at once after a poll that found something", async () => {
    const connector = new EmittingConnector();
    connector.pending.push([{ type: "term", data: "a" }], [{ type: "term", data: "b" }]);
    const { waits, done, sleep } = countingSleep(() => true);
    const attachment = await attachConnector(new SessionHub(), "w1", connector, "hijack", { now: () => 5, sleep });
    await done;
    await attachment.detach();
    // Two polls with output and no wait between them; the first wait comes
    // after the third poll, the first to find nothing.
    expect(connector.polls).toBe(3);
    expect(waits).toStrictEqual([POLL_IDLE_MS]);
  });

  it("stops polling once detached, and drops what an in-flight poll returns after", async () => {
    const hub = new SessionHub();
    const { store, recording } = await pollRecording();
    let answer: (messages: WorkerMessage[]) => void = () => {};
    let polls = 0;
    const connector = new RecordingConnector();
    connector.pollMessages = () => {
      polls += 1;
      return new Promise<WorkerMessage[]>((resolve) => {
        answer = resolve;
      });
    };
    const attachment = await attachConnector(hub, "w1", connector, "hijack", { now: () => 5, recording });
    await vi.waitFor(() => expect(polls).toBe(1));

    await attachment.detach();
    answer([{ type: "snapshot", screen: "late", ts: 7 }]);
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(polls).toBe(1);
    expect(summarise(await store.getEntries("w1"))).toStrictEqual(["log_start", "runtime_started", "read:seed"]);
  });

  it("drops a failure an in-flight poll reports after it was detached", async () => {
    const hub = new SessionHub();
    const { store, recording } = await pollRecording();
    let fail: (error: Error) => void = () => {};
    const connector = new RecordingConnector();
    connector.pollMessages = () =>
      new Promise<WorkerMessage[]>((_resolve, reject) => {
        fail = reject;
      });
    const attachment = await attachConnector(hub, "w1", connector, "hijack", { now: () => 5, recording });
    await attachment.detach();
    fail(new Error("socket closed"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(await store.getEntries("w1", { event: "runtime_error" })).toStrictEqual([]);
  });

  it("records a poll that fails and retries it on the reference's backoff", async () => {
    const { store, recording } = await pollRecording();
    const outcomes: Array<WorkerMessage[] | Error> = [
      new Error("read failed"),
      new Error("read failed again"),
      [{ type: "term", data: "back" }],
      new Error("and again"),
    ];
    const connector = new RecordingConnector();
    connector.pollMessages = async () => {
      const next = outcomes.shift() ?? [];
      if (next instanceof Error) {
        throw next;
      }
      return next;
    };
    const { waits, done, sleep } = countingSleep(() => outcomes.length === 0);
    const attachment = await attachConnector(new SessionHub(), "w1", connector, "hijack", {
      now: () => 5,
      recording,
      sleep,
    });
    await done;
    await attachment.detach();

    // Failures in a row back off further each time; a poll that works starts
    // the schedule over.
    expect(POLL_ERROR_BACKOFF_MS).toStrictEqual([250, 500, 1000, 2000, 5000]);
    expect(waits.slice(0, 3)).toStrictEqual([250, 500, 250]);
    const errors = await store.getEntries("w1", { event: "runtime_error" });
    expect(errors.map((entry) => entry.data)).toStrictEqual([
      { error: "read failed" },
      { error: "read failed again" },
      { error: "and again" },
    ]);
  });

  it("keeps polling after a failure when nothing records it", async () => {
    let polls = 0;
    const connector = new RecordingConnector();
    connector.pollMessages = async () => {
      polls += 1;
      throw new Error("read failed");
    };
    const { waits, done, sleep } = countingSleep((seen) => seen.length === 2);
    const attachment = await attachConnector(new SessionHub(), "w1", connector, "hijack", { now: () => 5, sleep });
    await done;
    await attachment.detach();
    expect(waits.slice(0, 2)).toStrictEqual([250, 500]);
    expect(polls).toBeGreaterThanOrEqual(2);
  });

  it("caps the backoff at its last step, whatever was thrown", async () => {
    const connector = new RecordingConnector();
    connector.pollMessages = async () => {
      throw "not even an Error";
    };
    const { store, recording } = await pollRecording();
    const { waits, done, sleep } = countingSleep((seen) => seen.length === 7);
    const attachment = await attachConnector(new SessionHub(), "w1", connector, "hijack", {
      now: () => 5,
      recording,
      sleep,
    });
    await done;
    await attachment.detach();
    expect(waits.slice(0, 7)).toStrictEqual([250, 500, 1000, 2000, 5000, 5000, 5000]);
    const [first] = await store.getEntries("w1", { event: "runtime_error" });
    expect(first?.data).toStrictEqual({ error: "not even an Error" });
  });
});
