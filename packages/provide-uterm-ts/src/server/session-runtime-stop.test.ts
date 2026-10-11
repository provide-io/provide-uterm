//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Bringing sessions down when the recording cannot be closed, and keeping
 * what goes wrong while they run.
 *
 * The reference's `stop()` raises out of `_stop_recording` before it reaches
 * `_discard_connector` or the state update, so a final flush that fails leaks
 * the PTY or ssh process and leaves the session reporting "running,
 * connected". The port releases everything first and raises after.
 */

import { describe, expect, it, vi } from "vitest";
import type { SessionConnector, WorkerMessage } from "../connectors/index.ts";
import { InMemoryRecordingStore, type RecordingEvent } from "../recording/index.ts";
import { SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import { SessionHub } from "./session-hub.ts";
import { recordingSettingsFrom } from "./session-recording.ts";
import { SessionRegistry } from "./session-registry.ts";
import { SessionRuntimes } from "./session-runtime.ts";
import { sessionDefinitionFrom } from "./session-status.ts";

/** A store that refuses every write once told to, as a full disk would. */
class FailingStore extends InMemoryRecordingStore {
  failing = false;
  override async appendEvents(sessionId: string, events: readonly RecordingEvent[]): Promise<void> {
    if (this.failing) {
      throw new Error(`EACCES: ${sessionId}`);
    }
    await super.appendEvents(sessionId, events);
  }
}

/** A connector that counts its stops, and can be told to fail them or its polls. */
class CountingConnector implements SessionConnector {
  stopped = 0;
  failStop = false;
  pollError: Error | undefined;
  async start(): Promise<void> {}
  async stop(): Promise<void> {
    this.stopped += 1;
    if (this.failStop) {
      throw new Error("already gone");
    }
  }
  isConnected(): boolean {
    return this.stopped === 0;
  }
  async pollMessages(): Promise<WorkerMessage[]> {
    if (this.pollError !== undefined) {
      throw this.pollError;
    }
    // Never resolves: the poll loop has nothing to do in these tests.
    return new Promise(() => {});
  }
  async handleInput(): Promise<WorkerMessage[]> {
    return [];
  }
  async handleControl(): Promise<WorkerMessage[]> {
    return [];
  }
  async getSnapshot(): Promise<WorkerMessage> {
    return { type: "snapshot", screen: "$ ", ts: 1 };
  }
  async getAnalysis(): Promise<string> {
    return "";
  }
  readonly modes: string[] = [];
  async setMode(mode: string): Promise<WorkerMessage[]> {
    this.modes.push(mode);
    return [];
  }
  async clear(): Promise<WorkerMessage[]> {
    return [];
  }
}

/** Recording sessions over `store`, each with its own connector from `connectors`. */
function runtimesOver(store: InMemoryRecordingStore, connectors: Record<string, CountingConnector>) {
  const registry = new SessionRegistry(
    Object.keys(connectors).map((sessionId) =>
      sessionDefinitionFrom({ session_id: sessionId }, "2026-01-01T00:00:00.000Z"),
    ),
    true,
  );
  const runtimes = new SessionRuntimes(registry, new SessionHub(), {
    build: (sessionId) => connectors[sessionId] as CountingConnector,
    now: () => 1_700_000_000,
    recordingStore: store,
    recordingSettings: recordingSettingsFrom({
      ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
      flush_interval_s: 3600,
      // Held until the stop, so the stop's final flush is the write that fails.
      flush_batch_size: 1000,
    }),
  });
  return { registry, runtimes };
}

describe("a final flush that fails", () => {
  it("still stops every connector and marks every session stopped, then raises the first error", async () => {
    const store = new FailingStore();
    const one = new CountingConnector();
    const two = new CountingConnector();
    const { registry, runtimes } = runtimesOver(store, { one, two });
    await runtimes.start("one");
    await runtimes.start("two");

    store.failing = true;
    const raised = await runtimes.stopAll().then(
      () => undefined,
      (error: unknown) => error,
    );

    // The first session's error, as it was thrown: the second's is in its
    // own last_error.
    expect(raised).toBeInstanceOf(Error);
    expect((raised as Error).message).toBe("EACCES: one");

    expect([one.stopped, two.stopped]).toStrictEqual([1, 1]);
    for (const sessionId of ["one", "two"]) {
      expect(registry.status(sessionId)).toMatchObject({
        lifecycle_state: "stopped",
        connected: false,
        stopped_at: 1_700_000_000,
        last_error: `EACCES: ${sessionId}`,
      });
    }
    expect(runtimes.connector("one")).toBeUndefined();
    // Released, so the session can be brought up again.
    store.failing = false;
    await runtimes.start("one");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "running", last_error: null });
  });

  it("leaves no error behind when the recording closes cleanly", async () => {
    const { registry, runtimes } = runtimesOver(new FailingStore(), { one: new CountingConnector() });
    await runtimes.start("one");
    await expect(runtimes.stopAll()).resolves.toBeUndefined();
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: null });
  });

  it("goes down past a connector that fails to stop, as the reference's discard suppresses it", async () => {
    const connector = new CountingConnector();
    connector.failStop = true;
    const { registry, runtimes } = runtimesOver(new FailingStore(), { one: connector });
    await runtimes.start("one");
    await expect(runtimes.stopAll()).resolves.toBeUndefined();
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", connected: false });
  });
});

describe("telling a connector its mode changed", () => {
  it("reaches a running connector, and is no error for a session that is not up", async () => {
    const connector = new CountingConnector();
    const { runtimes } = runtimesOver(new FailingStore(), { one: connector });
    await expect(runtimes.setMode("one", "open")).resolves.toBeUndefined();
    await runtimes.start("one");
    await runtimes.setMode("one", "open");
    expect(connector.modes).toStrictEqual(["open"]);
    await runtimes.stopAll();
  });
});

describe("a session whose worker never attached", () => {
  it("is still stopped by a shutdown, with nothing to detach", async () => {
    const connector = new CountingConnector();
    connector.getSnapshot = async () => {
      throw new Error("no screen");
    };
    const { registry, runtimes } = runtimesOver(new FailingStore(), { one: connector });
    await runtimes.start("one");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "no screen" });

    await expect(runtimes.stopAll()).resolves.toBeUndefined();
    expect(connector.stopped).toBe(1);
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "no screen" });
  });
});

describe("a failure the running session survives", () => {
  it("is kept as last_error while the session stays up, as the reference's _last_error is", async () => {
    const connector = new CountingConnector();
    connector.pollError = new Error("pty read failed");
    const { registry, runtimes } = runtimesOver(new FailingStore(), { one: connector });
    await runtimes.start("one");
    await vi.waitFor(() => expect(registry.status("one")?.last_error).toBe("pty read failed"));
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "running", connected: true });
    connector.pollError = undefined;
    await runtimes.stopAll();
  });
});
