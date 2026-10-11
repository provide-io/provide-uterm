//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Snapshot-before-stream dedupe, keyed on the matched text.
 *
 * Mirrors the reference's `tests/server/test_read_path_dedupe.py`: a match a
 * snapshot recorded is consumed once by the very next stream chunk and then
 * forgotten, so it can never suppress a later, genuinely new match; and the
 * key is a digest of the rule and the full match, so two different
 * credentials of one rule are two annotations.
 */

import { describe, expect, it } from "vitest";
import { PatternDetector } from "../annotation/index.ts";
import { InMemoryRecordingStore } from "../recording/index.ts";
import { SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import { addBounded, MAX_READ_ANNOTATION_KEYS, recordingSettingsFrom, SessionRecording } from "./session-recording.ts";

const SUDO = "sudo command detected: sudo";
const RM = "Recursive force-remove detected: rm -rf";
const AWS = "AWS access key detected in read";
const KEY_A = "AKIAIOSFODNN7EXAMPLE"; // pragma: allowlist secret
const KEY_B = "AKIAJ2345678ABCDWXYZ"; // pragma: allowlist secret

/** An open recording with the reference's rules, flushed per entry. */
async function annotating() {
  const store = new InMemoryRecordingStore();
  const settings = recordingSettingsFrom({
    ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
    flush_interval_s: 3600,
    flush_batch_size: 1,
  });
  const recording = new SessionRecording("s1", store, settings, { detector: new PatternDetector() });
  await recording.start(true);
  const term = (data: string) => recording.logOutbound({ type: "term", data });
  const snapshot = (screen: string) => recording.logOutbound({ type: "snapshot", screen });
  const descriptions = async () =>
    (await store.getEntries("s1", { event: "annotation" })).map((entry) =>
      String((entry.data as Record<string, unknown>).description),
    );
  return { store, recording, term, snapshot, descriptions };
}

describe("a match a snapshot recorded before the stream delivered it", () => {
  it("is consumed once by the next chunk, and a later repeat is a new occurrence", async () => {
    const { term, snapshot, descriptions } = await annotating();
    await snapshot("$ sudo ls\n");
    await term("$ sudo ls\r\n");
    expect(await descriptions()).toStrictEqual([SUDO]);

    await term("$ sudo ls\r\n");
    await snapshot("$ sudo ls\n$ sudo ls\n");
    expect(await descriptions()).toStrictEqual([SUDO, SUDO]);
  });

  it("does not stop a later, new match being recorded", async () => {
    const { term, snapshot, descriptions } = await annotating();
    await snapshot("$ sudo ls\n");
    await term("$ sudo ls\r\n");
    expect(await descriptions()).toStrictEqual([SUDO]);
    await term("$ sudo reboot\r\n");
    expect(await descriptions()).toStrictEqual([SUDO, SUDO, "reboot command detected: reboot"]);
  });

  it("expires with the next chunk even when that chunk matches nothing", async () => {
    const { term, snapshot, descriptions } = await annotating();
    await snapshot("$ sudo ls\n");
    await term("unrelated output\r\n");
    await term("$ sudo ls\r\n");
    expect(await descriptions()).toStrictEqual([SUDO, SUDO]);
  });

  it("expires with a chunk that is all held-back escape, too", async () => {
    const { term, snapshot, descriptions } = await annotating();
    await snapshot("$ sudo ls\n");
    // Not empty, so a chunk: the text it leaves to scan is empty.
    await term("\x1b[1");
    await term("m$ sudo ls\r\n");
    expect(await descriptions()).toStrictEqual([SUDO, SUDO]);
  });

  it("is consumed only by its own match", async () => {
    const { term, snapshot, descriptions } = await annotating();
    await snapshot("$ sudo ls\n");
    await term("$ rm -rf build\r\n");
    // And then expired, so the next sudo is recorded.
    await term("$ sudo ls\r\n");
    expect(await descriptions()).toStrictEqual([SUDO, RM, SUDO]);
  });

  it("survives an empty chunk, which is not a chunk", async () => {
    const { term, snapshot, descriptions } = await annotating();
    await snapshot("$ sudo ls\n");
    await term("");
    await term("$ sudo ls\r\n");
    expect(await descriptions()).toStrictEqual([SUDO]);
  });

  it("is forgotten when the recording stops", async () => {
    const { recording, term, snapshot, descriptions } = await annotating();
    await snapshot("$ sudo ls\n");
    await recording.stop();
    await recording.start(true);
    await term("$ sudo ls\r\n");
    expect(await descriptions()).toStrictEqual([SUDO, SUDO]);
  });
});

describe("different credentials of one rule", () => {
  it("are each recorded across successive snapshots, and a repeat is not", async () => {
    const { snapshot, descriptions } = await annotating();
    await snapshot(`${KEY_A} first\n`);
    await snapshot(`${KEY_B} second\n`);
    await snapshot(`${KEY_B} second\n`);
    expect(await descriptions()).toStrictEqual([AWS, AWS]);
  });

  it("are each recorded on the stream after a snapshot", async () => {
    const { term, snapshot, descriptions } = await annotating();
    await snapshot(`${KEY_A}\n`);
    await term(`${KEY_B}\r\n`);
    expect(await descriptions()).toStrictEqual([AWS, AWS]);
  });
});

describe("the bound on a key set", () => {
  it("clears a full set before adding, so the snapshot-only set is bounded too", () => {
    const keys = new Set<string>();
    for (let index = 0; index < MAX_READ_ANNOTATION_KEYS; index += 1) {
      addBounded(keys, `k${index}`);
    }
    expect(keys.size).toBe(MAX_READ_ANNOTATION_KEYS);
    addBounded(keys, "next");
    expect([...keys]).toStrictEqual(["next"]);
  });
});
