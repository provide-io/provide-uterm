//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Reading what a recording annotates as text: escape codes stripped, split
 * sequences rejoined, and each read-path match recorded once.
 *
 * Ports the reference's `_split_incomplete_escape`, `_read_annotation_key`,
 * `_remember_read_annotation` and the ANSI-stripped `_log_snapshot`
 * (runtime.py / runtime_helpers.py). The end-to-end sequence is also in
 * `session_recording_golden.json`'s annotated script; these pin each rule on
 * its own, including the bounds the corpus does not reach.
 */

import { describe, expect, it } from "vitest";
import { type Annotation, PatternDetector } from "../annotation/index.ts";
import { InMemoryRecordingStore } from "../recording/index.ts";
import { SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import {
  MAX_ESCAPE_CARRY,
  MAX_READ_ANNOTATION_KEYS,
  readAnnotationKey,
  recordingSettingsFrom,
  SessionRecording,
  splitIncompleteEscape,
} from "./session-recording.ts";

const ESC = "\x1b";

/** An open recording with the reference's rules (or `detector`), flushed per entry. */
async function annotating(detector: PatternDetector = new PatternDetector()) {
  const store = new InMemoryRecordingStore();
  const settings = recordingSettingsFrom({
    ...(SERVER_CONFIG_DEFAULTS.recording as Record<string, unknown>),
    flush_interval_s: 3600,
    flush_batch_size: 1,
  });
  const recording = new SessionRecording("s1", store, settings, { detector });
  await recording.start(true);
  return { store, recording };
}

/** The description of every annotation recorded, in order. */
async function descriptions(store: InMemoryRecordingStore): Promise<string[]> {
  const entries = await store.getEntries("s1", { event: "annotation" });
  return entries.map((entry) => String((entry.data as Record<string, unknown>).description));
}

describe("splitting off an unterminated escape sequence", () => {
  it.each([
    ["", "", ""],
    ["plain", "plain", ""],
    [`a${ESC}`, "a", ESC],
    [`a${ESC}[`, "a", `${ESC}[`],
    [`a${ESC}[1`, "a", `${ESC}[1`],
    // Every parameter byte, 0x30-0x3f, private markers included.
    [`a${ESC}[0123456789:;<=>?`, "a", `${ESC}[0123456789:;<=>?`],
    // Intermediate bytes, 0x20-0x2f, after the parameters.
    [`a${ESC}[1 !"#$%&'()*+,-./`, "a", `${ESC}[1 !"#$%&'()*+,-./`],
    // Only the last ESC can open one: a complete sequence before it stays.
    [`${ESC}[1mx${ESC}[2`, `${ESC}[1mx`, `${ESC}[2`],
  ])("%j splits to %j + %j", (text, complete, carry) => {
    expect(splitIncompleteEscape(text)).toStrictEqual([complete, carry]);
  });

  it.each([
    // Complete: strip_ansi will strip it as it is.
    `a${ESC}[1m`,
    `a${ESC}[1;2 q`,
    // Text after a complete sequence.
    `a${ESC}[1mtext`,
    // A two-character form, already complete.
    `a${ESC}M`,
    // A parameter byte after an intermediate one is not CSI grammar.
    `a${ESC}[ 1`,
    // Not a CSI: an OSC is not stripped as one either, so it is not held.
    `a${ESC}]0;title`,
  ])("keeps %j whole, as nothing about it is waiting for more", (text) => {
    expect(splitIncompleteEscape(text)).toStrictEqual([text, ""]);
  });

  it("carries at most the reference's 64 characters, ESC included", () => {
    expect(MAX_ESCAPE_CARRY).toBe(64);
    const longest = `${ESC}[${"1".repeat(MAX_ESCAPE_CARRY - 2)}`;
    expect(splitIncompleteEscape(`x${longest}`)).toStrictEqual(["x", longest]);
    const tooLong = `${longest}1`;
    expect(splitIncompleteEscape(`x${tooLong}`)).toStrictEqual([`x${tooLong}`, ""]);
  });
});

describe("the identity of a read-path annotation", () => {
  it("is the rule's label and its formatted description, NUL-separated", () => {
    const annotation: Annotation = {
      label: "privilege_escalation",
      description: "sudo command detected: sudo",
      severity: "warning",
      source: "detector",
      principal: "",
    };
    expect(readAnnotationKey(annotation)).toBe("privilege_escalation\x00sudo command detected: sudo");
  });
});

describe("a snapshot read as text", () => {
  it("masks what is typed next when the prompt row ends in a reset", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "snapshot", screen: `Password: ${ESC}[0m` });
    await recording.logSend("hunter2\r");
    const [send] = await store.getEntries("s1", { event: "send" });
    expect(send?.data).toStrictEqual({ keys: "***", bytes_b64: "Kioq", masked: true, byte_count: 8 });
  });

  it("finds a styled match the escape codes would otherwise split", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "snapshot", screen: `${ESC}[1msudo${ESC}[0m rm` });
    expect(await descriptions(store)).toStrictEqual(["sudo command detected: sudo"]);
  });

  it("is still recorded as it was sent, codes and all", async () => {
    const { store, recording } = await annotating();
    const screen = `${ESC}[1msudo${ESC}[0m rm`;
    await recording.logOutbound({ type: "snapshot", screen });
    const [read] = await store.getEntries("s1", { event: "read" });
    expect(read?.data).toMatchObject({ screen });
  });
});

describe("streamed output split inside an escape sequence", () => {
  it("is rejoined before stripping, so the styled word after it still matches", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "term", data: `$ ${ESC}[1` });
    await recording.logOutbound({ type: "term", data: `msudo${ESC}[0m rm x\r\n` });
    expect(await descriptions(store)).toStrictEqual(["sudo command detected: sudo"]);
  });

  it("keeps the carried half across an empty frame", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "term", data: `$ ${ESC}[1` });
    await recording.logOutbound({ type: "term", data: "" });
    await recording.logOutbound({ type: "term", data: "msudo x" });
    expect(await descriptions(store)).toStrictEqual(["sudo command detected: sudo"]);
  });

  it("starts each recording without the last one's half-sequence", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "term", data: `$ ${ESC}[1` });
    await recording.stop();
    await recording.start(true);
    // Unjoined, the chunk reads "msudo": no word boundary, no match.
    await recording.logOutbound({ type: "term", data: "msudo x" });
    expect(await descriptions(store)).toStrictEqual([]);
  });
});

describe("recording each read-path match once", () => {
  it("skips a snapshot match the stream already recorded", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "term", data: "$ sudo rm x\r\n" });
    await recording.logOutbound({ type: "snapshot", screen: "$ sudo rm x\n$ " });
    expect(await descriptions(store)).toStrictEqual(["sudo command detected: sudo"]);
  });

  it("skips a match an identical earlier snapshot recorded", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "snapshot", screen: "# rm -rf /tmp/x" });
    await recording.logOutbound({ type: "snapshot", screen: "# rm -rf /tmp/x" });
    expect(await descriptions(store)).toStrictEqual(["Recursive force-remove detected: rm -rf"]);
  });

  it("never suppresses the stream: a command run twice is annotated twice", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "snapshot", screen: "$ sudo ls" });
    await recording.logOutbound({ type: "term", data: "$ sudo ls\r\n" });
    await recording.logOutbound({ type: "term", data: "$ sudo ls\r\n" });
    expect(await descriptions(store)).toStrictEqual([
      "sudo command detected: sudo",
      "sudo command detected: sudo",
      "sudo command detected: sudo",
    ]);
  });

  it("does not dedupe typed input against what was read", async () => {
    const { store, recording } = await annotating();
    await recording.logSend("sudo ls\r");
    await recording.logOutbound({ type: "snapshot", screen: "$ sudo ls" });
    expect(await descriptions(store)).toStrictEqual(["sudo command detected: sudo", "sudo command detected: sudo"]);
  });

  it("annotates afresh in the next recording", async () => {
    const { store, recording } = await annotating();
    await recording.logOutbound({ type: "snapshot", screen: "$ sudo ls" });
    await recording.stop();
    await recording.start(true);
    await recording.logOutbound({ type: "snapshot", screen: "$ sudo ls" });
    expect(await descriptions(store)).toStrictEqual(["sudo command detected: sudo", "sudo command detected: sudo"]);
  });

  it("forgets everything once it holds the reference's 1024 keys, and starts over", async () => {
    // One annotation per word, keyed by the word, so the set fills exactly.
    const each = (text: string): Annotation[] =>
      text
        .split(" ")
        .filter((word) => word !== "")
        .map((word) => ({ label: "word", description: word, severity: "info", source: "t", principal: "" }));
    const detector = {
      detect: (_eventType: string, text: string) => each(text),
      scan: (_eventType: string, text: string) => ({ annotations: each(text), matchEnd: text.length }),
    } as unknown as PatternDetector;
    const { store, recording } = await annotating(detector);

    expect(MAX_READ_ANNOTATION_KEYS).toBe(1024);
    await recording.logOutbound({ type: "snapshot", screen: "k0" });
    const rest = Array.from({ length: MAX_READ_ANNOTATION_KEYS - 1 }, (_, index) => `k${index + 1}`);
    await recording.logOutbound({ type: "term", data: `${rest.join(" ")} ` });
    // Full, and k0 is still in it.
    await recording.logOutbound({ type: "snapshot", screen: "k0" });
    // One more key clears the set before it is added.
    await recording.logOutbound({ type: "term", data: "overflow " });
    await recording.logOutbound({ type: "snapshot", screen: "k0" });

    // The tail: the full set skipped the second k0, the cleared one did not.
    const tail = await store.getEntries("s1", { event: "annotation", limit: 3 });
    expect(tail.map((entry) => (entry.data as Record<string, unknown>).description)).toStrictEqual([
      `k${MAX_READ_ANNOTATION_KEYS - 1}`,
      "overflow",
      "k0",
    ]);
  });
});
