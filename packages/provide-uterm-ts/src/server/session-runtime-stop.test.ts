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
  const hub = new SessionHub();
  const runtimes = new SessionRuntimes(registry, hub, {
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
  return { registry, runtimes, hub };
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
  it("is let go at once, so a shutdown has nothing left to stop", async () => {
    const connector = new CountingConnector();
    connector.getSnapshot = async () => {
      throw new Error("no screen");
    };
    const { registry, runtimes } = runtimesOver(new FailingStore(), { one: connector });
    await runtimes.start("one");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "no screen" });
    expect(connector.stopped).toBe(1);

    await expect(runtimes.stopAll()).resolves.toBeUndefined();
    expect(connector.stopped).toBe(1);
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "no screen" });
  });
});

describe("a start that fails after its connector came up", () => {
  /** The events a session's recording holds, in order. */
  async function recorded(store: InMemoryRecordingStore, sessionId: string): Promise<string[]> {
    return (await store.getEntries(sessionId)).map((entry) => String(entry.event));
  }

  it("stops the connector and lets go of it when the recording cannot open", async () => {
    const store = new FailingStore();
    const realStart = store.startSession.bind(store);
    store.startSession = async () => {
      throw new Error("EROFS: read-only file system");
    };
    const connector = new CountingConnector();
    const { registry, runtimes, hub } = runtimesOver(store, { one: connector });

    await runtimes.start("one");

    expect(registry.status("one")).toMatchObject({
      lifecycle_state: "stopped",
      connected: false,
      last_error: "EROFS: read-only file system",
      stopped_at: 1_700_000_000,
    });
    expect(connector.stopped).toBe(1);
    expect(runtimes.connector("one")).toBeUndefined();
    // Never attached, so never on the hub.
    expect(hub.registry.get("one")).toBeUndefined();

    // Let go, so the next start is a real one rather than an early return.
    store.startSession = realStart;
    await runtimes.start("one");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "running", connected: true, last_error: null });
    expect(runtimes.connector("one")).toBe(connector);
    await runtimes.stopAll();
  });

  it("takes the worker back off the hub, closes the recording and stops the connector when attaching fails", async () => {
    const store = new FailingStore();
    const connector = new CountingConnector();
    const snapshot = connector.getSnapshot.bind(connector);
    connector.getSnapshot = async () => {
      throw new Error("no screen");
    };
    const { registry, runtimes, hub } = runtimesOver(store, { one: connector });

    await runtimes.start("one");

    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "no screen" });
    expect(hub.registry.get("one")).toBeUndefined();
    expect(connector.stopped).toBe(1);
    expect(runtimes.connector("one")).toBeUndefined();
    expect(await recorded(store, "one")).toStrictEqual(["log_start", "runtime_started", "log_stop"]);

    connector.getSnapshot = snapshot;
    await runtimes.start("one");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "running", connected: true, last_error: null });
    expect(hub.registry.get("one")?.workerWs).toBeDefined();
    await runtimes.stopAll();
  });

  it("still reports the failure when the connector cannot be stopped either", async () => {
    const connector = new CountingConnector();
    connector.failStop = true;
    connector.getSnapshot = async () => {
      throw new Error("no screen");
    };
    const { registry, runtimes } = runtimesOver(new FailingStore(), { one: connector });

    await expect(runtimes.start("one")).resolves.toBeUndefined();

    expect(connector.stopped).toBe(1);
    expect(runtimes.connector("one")).toBeUndefined();
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "no screen" });
  });
});

/** A promise to resolve by hand, and one that settles when it is first awaited on. */
function gate() {
  let open: () => void = () => {};
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  let reach: () => void = () => {};
  const reached = new Promise<void>((resolve) => {
    reach = resolve;
  });
  return {
    opened,
    open,
    reached,
    /** Note it was reached, then wait to be let through. */
    wait: () => {
      reach();
      return opened;
    },
  };
}

describe("a shutdown while a session is still attaching", () => {
  it("takes the worker off the hub, closes the recording and stops the connector, and says nothing failed", async () => {
    const store = new FailingStore();
    const connector = new CountingConnector();
    const snapshot = gate();
    // The seed screen never arrives, so the attachment never finishes.
    connector.getSnapshot = () => {
      void snapshot.wait();
      return new Promise(() => {});
    };
    const { registry, runtimes, hub } = runtimesOver(store, { one: connector });
    void runtimes.start("one");
    await snapshot.reached;
    expect(hub.registry.get("one")?.workerWs).toBeDefined();

    await expect(runtimes.stopAll()).resolves.toBeUndefined();
    expect(connector.stopped).toBe(1);
    expect(runtimes.connector("one")).toBeUndefined();
    expect(hub.registry.get("one")).toBeUndefined();
    expect((await store.getEntries("one")).map((entry) => entry.event).at(-1)).toBe("log_stop");
    expect(registry.status("one")).toMatchObject({
      lifecycle_state: "stopped",
      connected: false,
      last_error: null,
      stopped_at: 1_700_000_000,
    });
  });

  it("is waited out while the connector is still starting, which then comes straight back down", async () => {
    const store = new FailingStore();
    const connector = new CountingConnector();
    const starting = gate();
    connector.start = () => starting.wait();
    const { registry, runtimes, hub } = runtimesOver(store, { one: connector });
    const started = runtimes.start("one");
    await starting.reached;

    let stopped = false;
    const stopping = runtimes.stopAll().then(() => {
      stopped = true;
    });
    await new Promise((resolve) => setImmediate(resolve));
    // Not done: the connector's own start cannot be cancelled, only waited for.
    expect(stopped).toBe(false);
    starting.open();
    await stopping;
    await started;

    expect(connector.stopped).toBe(1);
    expect(runtimes.connector("one")).toBeUndefined();
    expect(hub.registry.get("one")).toBeUndefined();
    // Never opened: the shutdown came before the recording would have.
    expect(await store.getEntries("one")).toStrictEqual([]);
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", connected: false, last_error: null });
  });

  it("closes a recording that opened while it was being stopped", async () => {
    const store = new FailingStore();
    const opening = gate();
    const startSession = store.startSession.bind(store);
    store.startSession = async (sessionId, metadata) => {
      await opening.wait();
      await startSession(sessionId, metadata);
    };
    const connector = new CountingConnector();
    const { registry, runtimes, hub } = runtimesOver(store, { one: connector });
    void runtimes.start("one");
    await opening.reached;
    const stopping = runtimes.stopAll();
    opening.open();
    await stopping;

    expect(connector.stopped).toBe(1);
    expect(hub.registry.get("one")).toBeUndefined();
    expect((await store.getEntries("one")).map((entry) => entry.event)).toStrictEqual(["log_start", "log_stop"]);
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: null });
  });

  it("takes back an attachment that finished just as the shutdown began", async () => {
    const store = new FailingStore();
    const connector = new CountingConnector();
    const { registry, runtimes, hub } = runtimesOver(store, { one: connector });
    let stopping: Promise<void> | undefined;
    // The poll loop's first poll is made as the attachment returns: the
    // shutdown starts there, after the attachment can see it.
    connector.pollMessages = () => {
      stopping ??= runtimes.stopAll();
      return new Promise(() => {});
    };
    await runtimes.start("one");
    await stopping;

    expect(connector.stopped).toBe(1);
    expect(runtimes.connector("one")).toBeUndefined();
    expect(hub.registry.get("one")).toBeUndefined();
    expect((await store.getEntries("one")).map((entry) => entry.event).at(-1)).toBe("log_stop");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", connected: false, last_error: null });
  });
});

describe("an attachment taken back as the shutdown began", () => {
  it("still comes down when taking it off the hub fails", async () => {
    const connector = new CountingConnector();
    const { registry, runtimes, hub } = runtimesOver(new FailingStore(), { one: connector });
    hub.pruneIfIdle = async () => {
      throw new Error("prune failed");
    };
    let stopping: Promise<void> | undefined;
    connector.pollMessages = () => {
      stopping ??= runtimes.stopAll();
      return new Promise(() => {});
    };
    await runtimes.start("one");
    await expect(stopping).resolves.toBeUndefined();
    expect(connector.stopped).toBe(1);
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: null });
  });
});

describe("a start for a session nobody defined", () => {
  it("touches no state at all", async () => {
    const { registry, runtimes } = runtimesOver(new FailingStore(), {});
    const setState = vi.spyOn(registry, "setState");
    await runtimes.start("ghost");
    expect(setState).not.toHaveBeenCalled();
  });
});

describe("two starts at once", () => {
  it("bring the session up once, the second joining the first", async () => {
    const store = new FailingStore();
    const connector = new CountingConnector();
    const starting = gate();
    connector.start = () => starting.wait();
    let built = 0;
    const registry = new SessionRegistry(
      [sessionDefinitionFrom({ session_id: "one" }, "2026-01-01T00:00:00.000Z")],
      true,
    );
    const runtimes = new SessionRuntimes(registry, new SessionHub(), {
      build: () => {
        built += 1;
        return connector;
      },
      now: () => 1_700_000_000,
      recordingStore: store,
    });

    const first = runtimes.start("one");
    const second = runtimes.start("one");
    expect(second).toBe(first);
    await starting.reached;
    starting.open();
    await Promise.all([first, second]);

    expect(built).toBe(1);
    expect(runtimes.connector("one")).toBe(connector);
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "running", connected: true });
    // A start once it is up is a no-op, not a join.
    await runtimes.start("one");
    expect(built).toBe(1);
    await runtimes.stopAll();
    expect(connector.stopped).toBe(1);
  });

  it("let a later start through once the first has failed", async () => {
    const connector = new CountingConnector();
    const snapshot = connector.getSnapshot.bind(connector);
    connector.getSnapshot = async () => {
      throw new Error("no screen");
    };
    const { registry, runtimes } = runtimesOver(new FailingStore(), { one: connector });
    await runtimes.start("one");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "no screen" });
    connector.getSnapshot = snapshot;
    await runtimes.start("one");
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "running" });
    await runtimes.stopAll();
  });
});

describe("a recording that fails as the session starts", () => {
  it("is reported as last_error, and the session runs anyway", async () => {
    const store = new FailingStore();
    store.failing = true;
    const connector = new CountingConnector();
    const registry = new SessionRegistry(
      [sessionDefinitionFrom({ session_id: "one" }, "2026-01-01T00:00:00.000Z")],
      true,
    );
    const hub = new SessionHub();
    // Every write flushed at once, so the attach's own writes are the ones
    // that fail.
    const flushing = new SessionRuntimes(registry, hub, {
      build: () => connector,
      now: () => 1_700_000_000,
      recordingStore: store,
      recordingSettings: recordingSettingsFrom({
        ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
        flush_interval_s: 3600,
        flush_batch_size: 1,
      }),
    });
    await flushing.start("one");
    expect(registry.status("one")).toMatchObject({
      lifecycle_state: "running",
      connected: true,
      last_error: "EACCES: one",
    });
    expect(hub.registry.get("one")?.workerWs).toBeDefined();
    store.failing = false;
    await flushing.stopAll();
  });
});

describe("a detach that fails on the way down", () => {
  it("still closes the recording and stops the connector, then raises the detach's error", async () => {
    const store = new FailingStore();
    const one = new CountingConnector();
    const two = new CountingConnector();
    const { registry, runtimes, hub } = runtimesOver(store, { one, two });
    await runtimes.start("one");
    await runtimes.start("two");
    hub.pruneIfIdle = async (workerId) => {
      throw new Error(`prune failed: ${workerId}`);
    };

    const raised = await runtimes.stopAll().then(
      () => undefined,
      (error: unknown) => error,
    );

    expect((raised as Error).message).toBe("prune failed: one");
    expect([one.stopped, two.stopped]).toStrictEqual([1, 1]);
    for (const sessionId of ["one", "two"]) {
      // The final flush and the closing entry, written past the detach.
      expect((await store.getEntries(sessionId)).map((entry) => entry.event).at(-1)).toBe("log_stop");
      expect(registry.status(sessionId)).toMatchObject({
        lifecycle_state: "stopped",
        connected: false,
        last_error: `prune failed: ${sessionId}`,
      });
    }
  });

  it("keeps the detach's error, the first, when the recording then fails to close too", async () => {
    const store = new FailingStore();
    const one = new CountingConnector();
    const { registry, runtimes, hub } = runtimesOver(store, { one });
    await runtimes.start("one");
    hub.pruneIfIdle = async () => {
      throw new Error("prune failed");
    };
    store.failing = true;

    await expect(runtimes.stopAll()).rejects.toThrow("prune failed");
    expect(one.stopped).toBe(1);
    expect(registry.status("one")).toMatchObject({ lifecycle_state: "stopped", last_error: "prune failed" });
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
