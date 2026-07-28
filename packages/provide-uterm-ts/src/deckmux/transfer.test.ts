//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { MAX_QUEUE_LENGTH, TransferManager, WARNING_LEAD_S } from "./index.ts";

interface TransferGolden {
  queues: Array<{
    name: string;
    chunks: string[];
    displays: string[];
    raw_length: number;
    raw_tail: string;
    after_flush: string;
  }>;
  auto: Array<{ name: string; idle: number; queued: string[]; warn: boolean; transfer: boolean }>;
  warning_sequence: Record<string, [boolean, boolean]>;
  modes: Record<string, unknown>;
  settings: Record<string, unknown>;
}

const golden = loadGolden<TransferGolden>("deckmux_transfer_golden.json");

/** The decision as the corpus records it. */
function decision(manager: TransferManager, idle: number, queued: string[]): [boolean, boolean] {
  const result = manager.checkAutoTransfer(idle, queued);
  return [result.shouldWarn, result.shouldTransfer];
}

describe("the keystroke queue", () => {
  it.each(golden.queues)("$name", (record) => {
    const manager = new TransferManager();
    const displays = record.chunks.map((chunk) => manager.queueKeystroke("u2", chunk));
    expect(displays).toStrictEqual(record.displays);
    const raw = manager.flushQueue("u2");
    expect(raw.length).toBe(record.raw_length);
    expect(raw.slice(-8)).toBe(record.raw_tail);
    expect(manager.getQueueDisplay("u2")).toBe(record.after_flush);
  });

  it("keeps the newest when it overflows", () => {
    // Somebody who cannot type yet still generates keystrokes. Keeping the
    // oldest would show them the start of what they typed minutes ago.
    const record = golden.queues.find((entry) => entry.name === "well over the bound");
    expect(record?.raw_length).toBe(MAX_QUEUE_LENGTH);
    expect(record?.raw_tail).toBe("bbbbbbbb");
  });

  it("bounds at exactly the recorded length", () => {
    expect(golden.queues.find((entry) => entry.name === "exactly the bound")?.raw_length).toBe(MAX_QUEUE_LENGTH);
    expect(golden.queues.find((entry) => entry.name === "one over the bound")?.raw_length).toBe(MAX_QUEUE_LENGTH);
    expect(MAX_QUEUE_LENGTH).toBe(golden.settings.max_queue_length);
  });

  it("accumulates across chunks", () => {
    // A terminal delivers a typed line one keystroke at a time.
    const record = golden.queues.find((entry) => entry.name === "several chunks");
    expect(record?.displays).toStrictEqual(["l", "ls", "ls -la"]);
  });

  it("shows what was typed rather than the raw bytes", () => {
    const record = golden.queues.find((entry) => entry.name === "an arrow key");
    expect(record?.displays).toStrictEqual(["↑"]);
  });

  it("empties on a flush", () => {
    const manager = new TransferManager();
    manager.queueKeystroke("u2", "ls");
    expect(manager.flushQueue("u2")).toBe("ls");
    expect(manager.flushQueue("u2")).toBe("");
  });

  it("empties on a clear without handing anything back", () => {
    const manager = new TransferManager();
    manager.queueKeystroke("u2", "ls");
    manager.clearQueue("u2");
    expect(manager.getQueueDisplay("u2")).toBe("");
  });

  it("says nothing about a user with no queue", () => {
    const manager = new TransferManager();
    expect(manager.getQueueDisplay("nobody")).toBe("");
    expect(manager.flushQueue("nobody")).toBe("");
    expect(() => manager.clearQueue("nobody")).not.toThrow();
  });

  it("keeps each user's queue separate", () => {
    // One person's typing must not appear as another's.
    const manager = new TransferManager();
    manager.queueKeystroke("u2", "ls");
    manager.queueKeystroke("u3", "rm");
    expect(manager.getQueueDisplay("u2")).toBe("ls");
    expect(manager.getQueueDisplay("u3")).toBe("rm");
  });
});

describe("deciding when control moves", () => {
  it.each(golden.auto)("$name", (record) => {
    const manager = new TransferManager();
    expect(decision(manager, record.idle, record.queued)).toStrictEqual([record.warn, record.transfer]);
  });

  it("does nothing when nobody is waiting", () => {
    // Handing control to an empty queue would leave the terminal orphaned.
    const record = golden.auto.find((entry) => entry.name === "nobody waiting");
    expect([record?.warn, record?.transfer]).toStrictEqual([false, false]);
  });

  it("warns at the lead-in and transfers at the threshold", () => {
    expect(golden.auto.find((entry) => entry.name === "exactly at the warning threshold")?.warn).toBe(true);
    expect(golden.auto.find((entry) => entry.name === "exactly at the transfer threshold")?.transfer).toBe(true);
    expect(WARNING_LEAD_S).toBe(golden.settings.warning_lead_s);
  });

  it("does not warn once it is transferring", () => {
    // The transfer is the notification.
    expect(golden.auto.find((entry) => entry.name === "exactly at the transfer threshold")?.warn).toBe(false);
    expect(golden.auto.find((entry) => entry.name === "past the transfer threshold")?.warn).toBe(false);
  });

  it("says nothing while the owner is still active", () => {
    expect(golden.auto.find((entry) => entry.name === "well within the window")?.warn).toBe(false);
  });
});

describe("the warning", () => {
  it("is sent once", () => {
    // Re-sent on every check it would be a notification storm.
    const manager = new TransferManager();
    expect(decision(manager, 25, ["u2"])).toStrictEqual(golden.warning_sequence.first);
    expect(decision(manager, 26, ["u2"])).toStrictEqual(golden.warning_sequence.second);
  });

  it("is re-armed when the owner comes back", () => {
    const manager = new TransferManager();
    decision(manager, 25, ["u2"]);
    manager.resetWarning();
    expect(decision(manager, 27, ["u2"])).toStrictEqual(golden.warning_sequence.after_reset);
  });

  it("is re-armed when nobody is waiting any more", () => {
    // The owner is no longer holding anybody up, so the next time somebody
    // queues they get a warning of their own.
    const manager = new TransferManager();
    decision(manager, 25, ["u2"]);
    decision(manager, 25, []);
    expect(decision(manager, 25, ["u2"])).toStrictEqual(golden.warning_sequence.after_empty_queue);
  });

  it("is re-armed by the transfer itself", () => {
    // Otherwise the next idle period transfers silently.
    const manager = new TransferManager();
    decision(manager, 25, ["u2"]);
    decision(manager, 45, ["u2"]);
    expect(decision(manager, 25, ["u2"])).toStrictEqual(golden.warning_sequence.after_transfer);
  });
});

describe("the settings", () => {
  it("defaults to showing rather than replaying", () => {
    // Replaying somebody's queued keystrokes runs them; showing them does
    // not, which is the safer default for a shared terminal.
    expect(new TransferManager().queueMode).toBe(golden.settings.default_mode);
    expect(new TransferManager().autoTransferEnabled).toBe(golden.settings.default_enabled);
  });

  it("is switched off by a zero or negative window", () => {
    expect(new TransferManager({ autoTransferIdleS: 0 }).autoTransferEnabled).toBe(golden.settings.zero_disables);
    expect(new TransferManager({ autoTransferIdleS: -1 }).autoTransferEnabled).toBe(golden.settings.negative_disables);
  });

  it("never moves control when it is off", () => {
    const manager = new TransferManager({ autoTransferIdleS: 0 });
    expect(decision(manager, 1000, ["u2"])).toStrictEqual(golden.settings.disabled_never_warns);
  });

  it("warns immediately when the window is shorter than the lead-in", () => {
    // Clamped rather than negative, so the warning fires at all.
    const manager = new TransferManager({ autoTransferIdleS: 5 });
    expect(decision(manager, 0, ["u2"])).toStrictEqual(golden.settings.short_window_warns_immediately);
  });

  it("still transfers at the short threshold", () => {
    const manager = new TransferManager({ autoTransferIdleS: 5 });
    expect(decision(manager, 5, ["u2"])).toStrictEqual(golden.settings.short_window_transfers);
  });
});

describe("the transfer message", () => {
  it("shows the queued keys in display mode", () => {
    // Shown, not run: the new owner sees what was typed while they waited.
    const manager = new TransferManager({ keystrokeQueueMode: "display" });
    manager.queueKeystroke("u2", "ls\r");
    expect(manager.buildTransferMessage("u1", "u2", "handover")).toStrictEqual(golden.modes.display);
    expect(manager.getQueueDisplay("u2")).toBe(golden.modes.display_queue_after);
  });

  it("hands over the raw keys in replay mode", () => {
    // The raw bytes, so they can be typed — the display symbols would be
    // literal arrows on the command line.
    const manager = new TransferManager({ keystrokeQueueMode: "replay" });
    manager.queueKeystroke("u2", "ls\r");
    expect(manager.buildTransferMessage("u1", "u2", "handover")).toStrictEqual(golden.modes.replay);
    expect(manager.getQueueDisplay("u2")).toBe(golden.modes.replay_queue_after);
  });

  it("distinguishes the two", () => {
    // Confusing them either loses the keystrokes or runs them.
    expect((golden.modes.display as Record<string, unknown>).queued_keys).toBe("ls↵");
    expect((golden.modes.replay as Record<string, unknown>).queued_keys).toBe("ls\r");
  });

  it("empties the queue either way", () => {
    // A queue that survived the transfer would be handed over twice.
    expect(golden.modes.display_queue_after).toBe("");
    expect(golden.modes.replay_queue_after).toBe("");
  });

  it("carries an empty queue when there is nothing waiting", () => {
    const manager = new TransferManager();
    expect(manager.buildTransferMessage("u1", "u2", "auto_idle")).toStrictEqual(golden.modes.empty);
  });

  it("does not hand over somebody else's queue", () => {
    // The new owner would otherwise be typing what a third person typed.
    const manager = new TransferManager({ keystrokeQueueMode: "replay" });
    manager.queueKeystroke("u3", "rm -rf /\r");
    expect(manager.buildTransferMessage("u1", "u2", "handover")).toStrictEqual(golden.modes.someone_elses_queue);
    expect(manager.getQueueDisplay("u3")).toBe(golden.modes.someone_elses_queue_survives);
  });
});
