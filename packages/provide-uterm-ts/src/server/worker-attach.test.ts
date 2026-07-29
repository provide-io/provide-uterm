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

import { describe, expect, it } from "vitest";
import type { SessionConnector, WorkerMessage } from "../connectors/index.ts";
import { ShellSessionConnector } from "../connectors/index.ts";
import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import { SessionHub } from "./session-hub.ts";
import { attachConnector, workerSnapshotFrame } from "./worker-attach.ts";

/** The socket the hub is holding for a worker, for a test that writes to it. */
function socketOf(hub: SessionHub, workerId: string) {
  const socket = hub.registry.get(workerId)?.workerWs;
  if (socket === undefined) {
    throw new Error("nothing attached");
  }
  return socket;
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
      ts: 42,
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
    expect(Number((await hub.getLastSnapshot("w1"))?.ts)).toBeGreaterThanOrEqual(before);
  });

  it("records a screen carrying no detected prompt without inventing one", async () => {
    const hub = new SessionHub();
    await attachConnector(hub, "w1", new RecordingConnector(), "hijack", { now: () => 5 });
    const events = hub.registry.get("w1")?.events.toArray() ?? [];
    expect(events[0]).toMatchObject({ type: "snapshot", data: { prompt_id: null, screen: "seed" } });
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
