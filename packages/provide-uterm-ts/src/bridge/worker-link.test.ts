//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  encodeBridgeFrame,
  isPermanentConnectError,
  RECONNECT_BACKOFF,
  reconnectDelay,
  toWsUrl,
  WorkerLink,
  type WorkerLinkTarget,
} from "./index.ts";

interface WorkerLinkGolden {
  reconnect_backoff: number[];
  permanent_statuses: number[];
  urls: Array<{ name: string; manager_url: string; path: string; url: string }>;
  frames: Array<{ name: string; message: Record<string, unknown>; encoded: string }>;
  resize_defaults: { cols: number; rows: number; min: number };
}

const golden = loadGolden<WorkerLinkGolden>("worker_link_golden.json");

/** A worker that records what the link asked of it. */
class FakeWorker implements WorkerLinkTarget {
  readonly sent: string[] = [];
  readonly sizes: Array<[number, number]> = [];
  readonly hijacks: boolean[] = [];
  steps = 0;
  snapshot: Record<string, unknown> | undefined = { screen: "hello", cols: 100, rows: 40 };
  /** Makes every call throw, as a dead session would. */
  broken = false;

  async send(data: string): Promise<void> {
    if (this.broken) {
      throw new Error("session gone");
    }
    this.sent.push(data);
  }

  async setSize(cols: number, rows: number): Promise<void> {
    if (this.broken) {
      throw new Error("session gone");
    }
    this.sizes.push([cols, rows]);
  }

  async setHijacked(enabled: boolean): Promise<void> {
    if (this.broken) {
      throw new Error("session gone");
    }
    this.hijacks.push(enabled);
  }

  async requestStep(): Promise<void> {
    if (this.broken) {
      throw new Error("session gone");
    }
    this.steps += 1;
  }

  getSnapshot(): Record<string, unknown> | undefined {
    return this.snapshot;
  }
}

/** A link over a recording worker. */
function build() {
  const worker = new FakeWorker();
  const sent: string[] = [];
  const link = new WorkerLink({
    workerId: "w1",
    managerUrl: "https://hub.example",
    worker,
    now: () => 1000,
  });
  return { worker, link, sent };
}

describe("toWsUrl", () => {
  it.each(golden.urls)("$name", (record) => {
    // An operator configures an HTTP URL and the bridge has to reach it over
    // WebSocket. Getting the secure swap wrong downgrades the connection to
    // plaintext without anyone noticing.
    expect(toWsUrl(record.manager_url, record.path)).toBe(record.url);
  });

  it("keeps https on wss", () => {
    const record = golden.urls.find((entry) => entry.name === "https");
    expect(record?.url.startsWith("wss://")).toBe(true);
  });

  it("only rewrites a scheme at the start", () => {
    // A host that merely contains "http" must not be mangled.
    const record = golden.urls.find((entry) => entry.name === "http inside the host is not rewritten");
    expect(record?.url).toBe("wss://http.example/ws/worker/w1/term");
  });

  it("leaves an unrecognised scheme alone", () => {
    // Including an upper-case one — the reference matches case-sensitively,
    // so a port that lower-cased first would rewrite what the reference does
    // not.
    const record = golden.urls.find((entry) => entry.name === "uppercase scheme is not rewritten");
    expect(record?.url.startsWith("HTTPS://")).toBe(true);
  });
});

describe("encodeBridgeFrame", () => {
  it.each(golden.frames)("$name", (record) => {
    expect(encodeBridgeFrame(record.message)).toBe(record.encoded);
  });
});

describe("reconnect policy", () => {
  it("uses the reference ladder", () => {
    expect([...RECONNECT_BACKOFF]).toStrictEqual(golden.reconnect_backoff);
  });

  it("saturates rather than growing without bound", () => {
    // A worker that has been down for hours should keep trying at a steady
    // rate, not drift towards never retrying.
    const last = golden.reconnect_backoff.at(-1) as number;
    expect(reconnectDelay(0)).toBe(golden.reconnect_backoff[0]);
    expect(reconnectDelay(golden.reconnect_backoff.length - 1)).toBe(last);
    expect(reconnectDelay(golden.reconnect_backoff.length)).toBe(last);
    expect(reconnectDelay(9999)).toBe(last);
  });

  it("treats an auth or routing rejection as permanent", () => {
    // These never resolve on their own, and a fleet retrying them forever is
    // a denial of service against its own hub.
    for (const status of golden.permanent_statuses) {
      expect(isPermanentConnectError(status)).toBe(true);
    }
  });

  it("treats anything else as worth retrying", () => {
    for (const status of [undefined, 200, 429, 500, 502, 503]) {
      expect(isPermanentConnectError(status)).toBe(false);
    }
  });
});

describe("WorkerLink control dispatch", () => {
  it("pauses the worker on a pause action", async () => {
    const { worker, link } = build();
    await link.handleControl({ type: "control", action: "pause" });
    expect(worker.hijacks).toStrictEqual([true]);
  });

  it("resumes the worker on a resume action", async () => {
    const { worker, link } = build();
    await link.handleControl({ type: "control", action: "resume" });
    expect(worker.hijacks).toStrictEqual([false]);
  });

  it("steps the worker on a step action", async () => {
    const { worker, link } = build();
    await link.handleControl({ type: "control", action: "step" });
    expect(worker.steps).toBe(1);
  });

  it("ignores an unknown control action", async () => {
    const { worker, link } = build();
    await link.handleControl({ type: "control", action: "explode" });
    expect(worker.hijacks).toStrictEqual([]);
    expect(worker.steps).toBe(0);
  });

  it("resizes the session", async () => {
    const { worker, link } = build();
    await link.handleControl({ type: "resize", cols: 120, rows: 40 });
    expect(worker.sizes).toStrictEqual([[120, 40]]);
  });

  it("falls back to the reference size when the wire sends nonsense", async () => {
    // These go straight into a PTY ioctl, so a malformed value has to become
    // a sane default rather than reaching the kernel.
    const { worker, link } = build();
    await link.handleControl({ type: "resize", cols: "wide", rows: 0 });
    expect(worker.sizes).toStrictEqual([[golden.resize_defaults.cols, golden.resize_defaults.rows]]);
  });

  it("accepts a numeric string size", async () => {
    const { worker, link } = build();
    await link.handleControl({ type: "resize", cols: "120", rows: "40" });
    expect(worker.sizes).toStrictEqual([[120, 40]]);
  });

  it("routes an unknown type to a registered handler", async () => {
    const { link } = build();
    const seen: Array<Record<string, unknown>> = [];
    link.registerMessageHandler("custom", async (message) => {
      seen.push(message);
    });
    await link.handleControl({ type: "custom", payload: 1 });
    expect(seen).toStrictEqual([{ type: "custom", payload: 1 }]);
  });

  it("routes a message with no type to the empty-string handler", async () => {
    // A frame arriving without a type still has to go somewhere rather than
    // crash the dispatch; the reference keys it on the empty string.
    const { link } = build();
    const seen: Array<Record<string, unknown>> = [];
    link.registerMessageHandler("", async (message) => {
      seen.push(message);
    });
    await link.handleControl({ payload: 1 });
    await link.handleControl({ type: null });
    expect(seen).toHaveLength(2);
  });

  it("stamps outbound frames with wall time by default", async () => {
    // Every other test pins the clock; the hub shows this timestamp to a
    // human, so the default has to be real time.
    const worker = new FakeWorker();
    const link = new WorkerLink({ workerId: "w1", managerUrl: "https://hub.example", worker });
    const outbound: Array<Record<string, unknown>> = [];
    link.onSend((message) => outbound.push(message));
    await link.handleControl({ type: "control", action: "pause" });
    expect(Math.abs(Number(outbound[0]?.ts) - Date.now() / 1000)).toBeLessThan(5);
  });

  it("ignores an unknown type with no handler", async () => {
    const { link } = build();
    await expect(link.handleControl({ type: "mystery" })).resolves.toBeUndefined();
  });

  it("does not let a custom handler take precedence over a built-in", async () => {
    // Otherwise an app could shadow pause and make the worker unstoppable.
    const { worker, link } = build();
    const seen: string[] = [];
    link.registerMessageHandler("control", async () => {
      seen.push("custom");
    });
    await link.handleControl({ type: "control", action: "pause" });
    expect(worker.hijacks).toStrictEqual([true]);
    expect(seen).toStrictEqual([]);
  });

  it("survives a custom handler that throws", async () => {
    const { link } = build();
    link.registerMessageHandler("custom", async () => {
      throw new Error("handler exploded");
    });
    await expect(link.handleControl({ type: "custom" })).resolves.toBeUndefined();
  });

  it("survives a worker that throws", async () => {
    // The session can die between a frame arriving and being applied; that
    // must not tear down the connection loop.
    const { worker, link } = build();
    worker.broken = true;
    await expect(link.handleControl({ type: "control", action: "pause" })).resolves.toBeUndefined();
    await expect(link.handleControl({ type: "resize", cols: 80, rows: 25 })).resolves.toBeUndefined();
    await expect(link.handleControl({ type: "control", action: "step" })).resolves.toBeUndefined();
  });

  it("reports a hijack change back to the hub", async () => {
    // The dashboard shows whether the worker actually paused, so the
    // acknowledgement matters as much as the action.
    const { link } = build();
    const outbound: Array<Record<string, unknown>> = [];
    link.onSend((message) => outbound.push(message));
    await link.handleControl({ type: "control", action: "pause" });
    expect(outbound).toStrictEqual([{ type: "status", hijacked: true, ts: 1000 }]);
  });
});

describe("WorkerLink data", () => {
  it("forwards terminal input to the session", async () => {
    const { worker, link } = build();
    await link.handleData("ls\r");
    expect(worker.sent).toStrictEqual(["ls\r"]);
  });

  it("ignores empty input", async () => {
    const { worker, link } = build();
    await link.handleData("");
    expect(worker.sent).toStrictEqual([]);
  });

  it("survives a session that refuses input", async () => {
    const { worker, link } = build();
    worker.broken = true;
    await expect(link.handleData("ls")).resolves.toBeUndefined();
  });
});

describe("WorkerLink snapshots", () => {
  it("answers a snapshot request", async () => {
    const { link } = build();
    const outbound: Array<Record<string, unknown>> = [];
    link.onSend((message) => outbound.push(message));
    await link.handleControl({ type: "snapshot_req" });
    expect(outbound).toHaveLength(1);
    expect(outbound[0]).toMatchObject({ type: "snapshot", screen: "hello", cols: 100, rows: 40, ts: 1000 });
  });

  it("fills in the reference defaults for a sparse snapshot", async () => {
    const { worker, link } = build();
    worker.snapshot = {};
    const outbound: Array<Record<string, unknown>> = [];
    link.onSend((message) => outbound.push(message));
    await link.handleControl({ type: "snapshot_req" });
    expect(outbound[0]).toMatchObject({
      screen: "",
      cols: golden.resize_defaults.cols,
      rows: golden.resize_defaults.rows,
      cursor: { x: 0, y: 0 },
      cursor_at_end: true,
      has_trailing_space: false,
    });
  });

  it("says nothing when the worker has no snapshot", async () => {
    const { worker, link } = build();
    worker.snapshot = undefined;
    const outbound: Array<Record<string, unknown>> = [];
    link.onSend((message) => outbound.push(message));
    await link.handleControl({ type: "snapshot_req" });
    expect(outbound).toStrictEqual([]);
  });
});

describe("WorkerLink disconnect", () => {
  it("releases the worker when the connection drops", async () => {
    // The hub clears its own hijack state but cannot send a resume over a
    // closed socket, so the worker has to un-pause itself or it stays frozen
    // forever.
    const { worker, link } = build();
    await link.handleControl({ type: "control", action: "pause" });
    await link.handleDisconnect();
    expect(worker.hijacks).toStrictEqual([true, false]);
  });

  it("releases even when the worker is failing", async () => {
    const { worker, link } = build();
    worker.broken = true;
    await expect(link.handleDisconnect()).resolves.toBeUndefined();
  });
});
