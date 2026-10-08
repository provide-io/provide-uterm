//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A bootstrapped server records the sessions its configuration says it does.
 *
 * Before this was wired, `recording_enabled` was read off the configuration
 * and reported to every client while nothing was ever written: the status
 * claimed a recording that did not exist.
 */

import { describe, expect, it } from "vitest";
import { encodeTerminalData } from "../control-channel/index.ts";
import { InMemoryRecordingStore, LocalFileRecordingStore } from "../recording/index.ts";
import { bootstrapServer, ServerBootstrapError } from "./bootstrap.ts";

const SESSION = { session_id: "one", connector_type: "shell", auto_start: true };

describe("recording from the configuration", () => {
  it("records to local files by default, as the reference does", () => {
    const { runtimes } = bootstrapServer({ authMode: "jwt" });
    expect(runtimes.recordingStore).toBeInstanceOf(LocalFileRecordingStore);
  });

  it("records a session the configuration enables, with the configured flush", async () => {
    const { runtimes, registry } = bootstrapServer({
      authMode: "jwt",
      document: {
        sessions: [SESSION],
        recording: { enabled_by_default: true, store_type: "memory", flush_batch_size: 1 },
      },
    });
    const store = runtimes.recordingStore as InMemoryRecordingStore;
    expect(store).toBeInstanceOf(InMemoryRecordingStore);

    await runtimes.startAutoStart();

    // A batch of one: written without anyone flushing.
    const events = (await store.getEntries("one")).map((entry) => entry.event);
    expect(events).toStrictEqual(["log_start", "runtime_started", "read"]);
    expect(registry.status("one")?.recording_enabled).toBe(true);
    await runtimes.stopAll();
    expect((await store.getEntries("one")).at(-1)?.event).toBe("log_stop");
  });

  it("records nothing for a session the configuration leaves off", async () => {
    const { runtimes, registry } = bootstrapServer({
      authMode: "jwt",
      document: { sessions: [SESSION], recording: { store_type: "memory" } },
    });
    await runtimes.startAutoStart();
    await runtimes.stopAll();
    expect(registry.status("one")?.recording_enabled).toBe(false);
    expect(await runtimes.recordingStore.getEntries("one")).toStrictEqual([]);
  });

  it("annotates what it records, as the reference's factory does for every runtime", async () => {
    const { runtimes, hub } = bootstrapServer({
      authMode: "jwt",
      document: { sessions: [SESSION], recording: { enabled_by_default: true, store_type: "memory" } },
    });
    await runtimes.startAutoStart();
    await hub.registry.get("one")?.workerWs?.sendText(encodeTerminalData("sudo ls\r"));
    await runtimes.flushRecording("one");
    const annotations = await runtimes.recordingStore.getEntries("one", { event: "annotation" });
    expect(annotations.map((entry) => (entry.data as Record<string, unknown>).label)).toContain("privilege_escalation");
    await runtimes.stopAll();
  });

  it("refuses to start with a webhook store it cannot deliver to", () => {
    expect(() =>
      bootstrapServer({
        authMode: "jwt",
        document: { recording: { store_type: "webhook", webhook_url: "https://hooks.example/r" } },
      }),
    ).toThrow(ServerBootstrapError);
  });
});
