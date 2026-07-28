//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  CLEAR_SCREEN,
  DEFAULT_REPLAY_EVENTS,
  rawBytesFromLog,
  rebuildRawStream,
  replayLog,
  replayLogText,
  SPEED_CEILING,
  SPEED_FLOOR,
  STEP_PROMPT,
} from "./index.ts";

interface ReplayGolden {
  hostile_lines: Record<string, { rebuild: string | null; replay: string | null }>;
  log_lines: string[];
  clear_screen: string;
  raw_stream: { bytes: number[]; text: string; empty_log_bytes: number[] };
  playback: Array<{ name: string; output: string; slept: number[]; prompts: string[] }>;
  default_events: string[];
  speed_floor: number;
  speed_ceiling: number;
}

const golden = loadGolden<ReplayGolden>("replay_golden.json");

const directory = mkdtempSync(join(tmpdir(), "uterm-replay-"));
afterAll(() => rmSync(directory, { recursive: true, force: true }));

const LOG_TEXT = `${golden.log_lines.join("\n")}\n`;

/** The options for a recorded playback, in the port's spelling. */
function playbackOptions(name: string) {
  const cases: Record<string, { speed?: number; step?: boolean; events?: string[] }> = {
    "real time": {},
    "double speed": { speed: 2.0 },
    "half speed": { speed: 0.5 },
    "faster than the ceiling": { speed: 1000.0 },
    "slower than the floor": { speed: 0.001 },
    zero: { speed: 0.0 },
    negative: { speed: -1.0 },
    stepping: { step: true },
    "only reads": { events: ["read"] },
    "an event that is not in the log": { events: ["keystroke"] },
    "every event named": { events: ["read", "screen", "write"] },
  };
  return cases[name] as { speed?: number; step?: boolean; events?: string[] };
}

/** Capture what a replay writes, sleeps for, and prompts with. */
function capture() {
  const written: string[] = [];
  const slept: number[] = [];
  const prompts: string[] = [];
  return {
    written,
    slept,
    prompts,
    options: {
      write: (text: string) => void written.push(text),
      sleep: async (seconds: number) => void slept.push(seconds),
      prompt: async (message: string) => void prompts.push(message),
    },
  };
}

/** Write a log file and return its path. */
function logFile(text: string, name: string): string {
  const path = join(directory, name);
  writeFileSync(path, text, "utf8");
  return path;
}

describe("rawBytesFromLog", () => {
  it("concatenates the bytes of every read", () => {
    // The rebuilt stream is what gets fed back through an emulator, so a
    // dropped or reordered chunk is a different session.
    expect([...rawBytesFromLog(LOG_TEXT)]).toStrictEqual(golden.raw_stream.bytes);
  });

  it("keeps them in the order the log has them", () => {
    expect(Buffer.from(rawBytesFromLog(LOG_TEXT)).toString()).toBe("hello world");
  });

  it("takes bytes from a read that carries no screen", () => {
    // The viewer skips that record; the rebuild must not, or the stream
    // loses everything the operator did not see rendered.
    expect(Buffer.from(rawBytesFromLog(LOG_TEXT)).toString()).toContain(" world");
  });

  it("ignores events that are not reads", () => {
    const onlyWrites = `${JSON.stringify({ event: "write", data: { raw_bytes_b64: "aGVsbG8=" } })}\n`;
    expect([...rawBytesFromLog(onlyWrites)]).toStrictEqual([]);
  });

  it("ignores a read with no data at all", () => {
    // A record that carries nothing is not a failure; it just adds nothing.
    const bare = `${JSON.stringify({ event: "read", ts: 1.0 })}\n`;
    expect([...rawBytesFromLog(bare)]).toStrictEqual([]);
  });

  it("ignores a read with no bytes at all", () => {
    const empty = `${JSON.stringify({ event: "read", data: { raw_bytes_b64: "" } })}\n`;
    expect([...rawBytesFromLog(empty)]).toStrictEqual([]);
  });

  it("gives an empty log an empty stream", () => {
    expect([...rawBytesFromLog("")]).toStrictEqual(golden.raw_stream.empty_log_bytes);
  });

  it("skips blank lines", () => {
    expect([...rawBytesFromLog("\n   \n\n")]).toStrictEqual([]);
  });

  it("fails on a line that is not JSON", () => {
    // Unlike the viewer, which skips it. The rebuild feeds an emulator, so a
    // silently short stream would be a silently different session.
    expect(golden.hostile_lines["corrupt json"]?.rebuild).not.toBeNull();
    expect(() => rawBytesFromLog("{not json\n")).toThrow();
  });

  it("fails on a line that is not an object", () => {
    for (const line of ["null", "[]", '"just a string"']) {
      expect(() => rawBytesFromLog(`${line}\n`)).toThrow(TypeError);
    }
  });

  it("fails on a read whose data is not a map", () => {
    // Reading it as an empty map would quietly drop whatever that record
    // carried, and the stream would be short by exactly that much.
    expect(golden.hostile_lines["data is a list"]?.rebuild).not.toBeNull();
    const line = JSON.stringify({ event: "read", ts: 1.0, data: ["not", "a", "map"] });
    expect(() => rawBytesFromLog(`${line}\n`)).toThrow(TypeError);
  });

  it("does not look at the data of an event it is skipping", () => {
    // A screen event's data is never read, so a malformed one there is not
    // this function's problem.
    expect(golden.hostile_lines["data is a string"]?.rebuild).toBeNull();
    const line = JSON.stringify({ event: "screen", ts: 1.0, data: "not a map" });
    expect([...rawBytesFromLog(`${line}\n`)]).toStrictEqual([]);
  });
});

describe("rebuildRawStream", () => {
  it("writes the stream to the destination", () => {
    const path = logFile(LOG_TEXT, "rebuild.jsonl");
    const out = join(directory, "rebuild.bin");
    rebuildRawStream(path, out);
    expect([...readFileSync(out)]).toStrictEqual(golden.raw_stream.bytes);
  });

  it("writes an empty file for an empty log", () => {
    const path = logFile("", "empty.jsonl");
    const out = join(directory, "empty.bin");
    rebuildRawStream(path, out);
    expect([...readFileSync(out)]).toStrictEqual(golden.raw_stream.empty_log_bytes);
  });
});

describe("replaying", () => {
  it.each(golden.playback)("$name", async (record) => {
    const captured = capture();
    await replayLogText(LOG_TEXT, { ...playbackOptions(record.name), ...captured.options });
    expect(captured.written.join("")).toBe(record.output);
    expect(captured.slept).toStrictEqual(record.slept);
    expect(captured.prompts).toStrictEqual(record.prompts);
  });

  it("reads the log from a file too", async () => {
    const captured = capture();
    await replayLog(logFile(LOG_TEXT, "playback.jsonl"), captured.options);
    const record = golden.playback.find((entry) => entry.name === "real time");
    expect(captured.written.join("")).toBe(record?.output);
  });
});

describe("which frames are shown", () => {
  /** The frames a recorded playback rendered. */
  function frames(name: string): string[] {
    const record = golden.playback.find((entry) => entry.name === name);
    return (record?.output ?? "").split(golden.clear_screen).slice(1);
  }

  it("defaults to reads and screens", () => {
    expect([...DEFAULT_REPLAY_EVENTS]).toStrictEqual(golden.default_events);
  });

  it("shows only the events asked for", () => {
    expect(frames("only reads")).toStrictEqual(["after a second and a half", "much later"]);
  });

  it("shows nothing when no event in the log is wanted", () => {
    expect(frames("an event that is not in the log")).toStrictEqual([]);
  });

  it("shows an event the default set leaves out when it is named", () => {
    expect(frames("every event named")).toContain("a write");
    expect(frames("real time")).not.toContain("a write");
  });

  it("shows a frame whose screen is empty", () => {
    // A cleared terminal is a real frame, and it is often the one just
    // before the interesting moment. Treating "" as absent drops it.
    expect(frames("real time")).toContain("");
  });

  it("skips a record with no screen at all", () => {
    // The read that carries only bytes: there is nothing to draw.
    expect(frames("real time")).toHaveLength(6);
    expect(frames("only reads")).toHaveLength(2);
  });

  it("clears the screen before every frame", () => {
    // Without the clear, a shorter frame leaves the tail of the longer one
    // behind it and the replay shows something that never appeared.
    const record = golden.playback.find((entry) => entry.name === "real time");
    expect(CLEAR_SCREEN).toBe(golden.clear_screen);
    expect(record?.output.startsWith(golden.clear_screen)).toBe(true);
    expect((record?.output ?? "").split(golden.clear_screen).length - 1).toBe(6);
  });

  it("writes the frame with nothing added", () => {
    // No trailing newline: the screen is already the right height, and one
    // more line would scroll the top off.
    expect(frames("only reads")[0]).toBe("after a second and a half");
  });
});

describe("the defaults", () => {
  // Every other test injects a sink, a sleep and a prompt. Without one case
  // that takes the real ones, a viewer that never waits — or one that writes
  // nowhere — would look fully covered.
  it("waits for real when given no sleep", async () => {
    // A default that did not wait would replay a whole session instantly,
    // and every timing test above would still pass.
    const log = [
      JSON.stringify({ event: "screen", ts: 0, data: { screen: "one" } }),
      JSON.stringify({ event: "screen", ts: 0.05, data: { screen: "two" } }),
    ].join("\n");
    const started = performance.now();
    await replayLogText(`${log}\n`, { write: () => undefined });
    expect(performance.now() - started).toBeGreaterThan(25);
  });

  it("writes to standard output and waits for real", async () => {
    const written: string[] = [];
    const original = process.stdout.write.bind(process.stdout);
    process.stdout.write = ((chunk: string) => {
      written.push(String(chunk));
      return true;
    }) as typeof process.stdout.write;
    try {
      // Two frames a millisecond apart, so the real sleep is taken and is
      // over almost at once.
      const log = [
        JSON.stringify({ event: "screen", ts: 0, data: { screen: "one" } }),
        JSON.stringify({ event: "screen", ts: 0.001, data: { screen: "two" } }),
      ].join("\n");
      await replayLogText(`${log}\n`, { step: false });
    } finally {
      process.stdout.write = original;
    }
    expect(written.join("")).toBe(`${CLEAR_SCREEN}one${CLEAR_SCREEN}two`);
  });

  it("does not wait for a caller that gave no prompt", async () => {
    // Stepping with no prompt would otherwise hang the replay for good.
    const written: string[] = [];
    await expect(
      replayLogText(`${JSON.stringify({ event: "screen", ts: 0, data: { screen: "one" } })}\n`, {
        step: true,
        write: (text) => void written.push(text),
      }),
    ).resolves.toBeUndefined();
    expect(written.join("")).toBe(`${CLEAR_SCREEN}one`);
  });
});

describe("the timing", () => {
  /** The delays a recorded playback slept for. */
  function slept(name: string): number[] {
    return golden.playback.find((entry) => entry.name === name)?.slept ?? [];
  }

  it("takes the delay from the log's own timestamps", async () => {
    expect(slept("real time")).toStrictEqual([1.5, 1.5, 8.0]);
  });

  it("never sleeps before the first frame", async () => {
    // There is nothing to be late for yet.
    expect(slept("real time")).toHaveLength(3);
  });

  it("divides by the speed", () => {
    expect(slept("double speed")).toStrictEqual(slept("real time").map((value) => value / 2));
    expect(slept("half speed")).toStrictEqual(slept("real time").map((value) => value * 2));
  });

  it("clamps a speed above the ceiling", () => {
    // Otherwise a large enough multiplier makes every delay zero and the
    // replay is just a flicker.
    expect(SPEED_CEILING).toBe(golden.speed_ceiling);
    expect(slept("faster than the ceiling")).toStrictEqual(
      slept("real time").map((value) => value / golden.speed_ceiling),
    );
  });

  it("clamps a speed below the floor", () => {
    expect(SPEED_FLOOR).toBe(golden.speed_floor);
    expect(slept("slower than the floor")).toStrictEqual(slept("real time").map((value) => value / golden.speed_floor));
  });

  it("treats zero as the floor rather than dividing by it", () => {
    expect(slept("zero")).toStrictEqual(slept("slower than the floor"));
  });

  it("treats a negative speed as the floor rather than playing backwards", () => {
    expect(slept("negative")).toStrictEqual(slept("slower than the floor"));
  });

  it("does not sleep when the timestamps go backwards", async () => {
    // A merged or clock-adjusted log has them, and a negative delay is not
    // something to wait out.
    const captured = capture();
    await replayLogText(LOG_TEXT, captured.options);
    expect(captured.slept.every((value) => value > 0)).toBe(true);
  });

  it("keeps the previous timestamp when a record has none", async () => {
    // Reading a missing timestamp as zero would make the next delay the
    // whole age of the log.
    expect(slept("real time")[0]).toBe(1.5);
  });
});

describe("stepping", () => {
  it("waits for the caller instead of sleeping", async () => {
    const record = golden.playback.find((entry) => entry.name === "stepping");
    expect(record?.slept).toStrictEqual([]);
    expect(record?.prompts).toHaveLength(6);
  });

  it("prompts once per frame", async () => {
    const captured = capture();
    await replayLogText(LOG_TEXT, { step: true, ...captured.options });
    expect(captured.prompts.every((prompt) => prompt === STEP_PROMPT)).toBe(true);
  });

  it("prompts after drawing, not before", async () => {
    // The point of a step is to look at the frame.
    const order: string[] = [];
    await replayLogText(LOG_TEXT, {
      step: true,
      write: (text) => void order.push(text === CLEAR_SCREEN ? "clear" : "frame"),
      prompt: async () => void order.push("prompt"),
    });
    expect(order.slice(0, 3)).toStrictEqual(["clear", "frame", "prompt"]);
  });
});

describe("lines that are not records", () => {
  it("skips a blank line", async () => {
    const captured = capture();
    await replayLogText("\n   \n", captured.options);
    expect(captured.written).toStrictEqual([]);
  });

  it("skips a line that is not JSON, and says so", async () => {
    // One corrupt line in the middle of an incident log must not end the
    // replay there.
    expect(golden.hostile_lines["corrupt json"]?.replay).toBeNull();
    const warnings: string[] = [];
    const captured = capture();
    await replayLogText(`{not json\n${golden.log_lines[1]}\n`, {
      ...captured.options,
      onWarning: (message) => void warnings.push(message),
    });
    expect(captured.written.length).toBeGreaterThan(0);
    expect(warnings).toHaveLength(1);
  });

  it("names the line it skipped", async () => {
    const warnings: string[] = [];
    await replayLogText(`\n{not json\n`, {
      write: () => undefined,
      onWarning: (message) => void warnings.push(message),
    });
    expect(warnings[0]).toContain("2");
  });

  it("does not require a warning sink", async () => {
    await expect(replayLogText("{not json\n", { write: () => undefined })).resolves.toBeUndefined();
  });

  it("fails on a record whose data is not a map", async () => {
    // Both readers reach into it; the viewer reaches even for a screen event,
    // which is where the two differ.
    expect(golden.hostile_lines["data is a string"]?.replay).not.toBeNull();
    const line = JSON.stringify({ event: "screen", ts: 1.0, data: "not a map" });
    await expect(replayLogText(`${line}\n`, { write: () => undefined })).rejects.toThrow(TypeError);
  });

  it("does not look at the data of an event it is skipping", async () => {
    // The event filter runs first, so a malformed record nobody asked for is
    // not the viewer's problem — and a log full of them still replays.
    expect(golden.hostile_lines["data is a string on an unwanted event"]?.replay).toBeNull();
    const line = JSON.stringify({ event: "write", ts: 1.0, data: "not a map" });
    const captured = capture();
    await expect(replayLogText(`${line}\n`, captured.options)).resolves.toBeUndefined();
    expect(captured.written).toStrictEqual([]);
  });

  it("fails on a line that is not an object", async () => {
    // Matching the reference, which reaches for `.get` on whatever the line
    // parsed to. Skipping it here would be a kindness the rebuild does not
    // share, and the two would disagree about the same file.
    for (const line of ["null", "[]", '"just a string"']) {
      expect(golden.hostile_lines[line === "null" ? "a json null" : "a json array"]?.replay).not.toBeNull();
      await expect(replayLogText(`${line}\n`, { write: () => undefined })).rejects.toThrow(TypeError);
    }
  });
});
