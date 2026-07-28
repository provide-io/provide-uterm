//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Terminal session replay viewer.
 *
 * Port of the Python module `provide.uterm.replay.viewer`.
 *
 * A replay is what an incident review actually watches, so two things matter
 * and both are quiet when wrong. Which frames are shown: a screen that is the
 * empty string is a real frame — a cleared terminal, often the one just
 * before the interesting moment — while a record with no screen at all has
 * nothing to draw. And the timing: delays come from the log's own timestamps,
 * divided by a speed that is clamped at both ends, because zero would divide
 * by zero and a negative one would play backwards.
 */

import { readFileSync } from "node:fs";

/** What the terminal is reset with before each frame. */
export const CLEAR_SCREEN = "\x1b[2J\x1b[H";

/** The events rendered when the caller names none. */
export const DEFAULT_REPLAY_EVENTS: readonly string[] = ["read", "screen"];

/** Slowest playback, so a speed of zero cannot stop it dead. */
export const SPEED_FLOOR = 0.01;

/** Fastest playback, so a large multiplier cannot make it a flicker. */
export const SPEED_CEILING = 100.0;

/** What the caller is asked between frames when stepping. */
export const STEP_PROMPT = "-- next --";

/** How a replay is driven. */
export interface ReplayOptions {
  /** Playback multiplier. Clamped to [{@link SPEED_FLOOR}, {@link SPEED_CEILING}]. */
  speed?: number;
  /** Wait for the caller between frames instead of sleeping. */
  step?: boolean;
  /** Which events to render. Defaults to {@link DEFAULT_REPLAY_EVENTS}. */
  events?: string[];
  /** Where frames go. */
  write?: (text: string) => void;
  /** How a delay is taken. */
  sleep?: (seconds: number) => Promise<void>;
  /** How the caller is asked to advance when stepping. */
  prompt?: (message: string) => Promise<void>;
  /** Told about a line that could not be read. */
  onWarning?: (message: string) => void;
}

/** One record, as much of one as this reads. */
interface LogRecord {
  event?: unknown;
  ts?: unknown;
  data?: unknown;
}

/** Wait for `seconds`. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

/**
 * The `data` map of a record.
 *
 * Absent is an empty map; present but not a map is a failure, because the
 * reference reaches into it and raises there. Reading a list as an empty map
 * would quietly drop whatever that record carried.
 *
 * @throws {TypeError} When `data` is present and is not an object.
 */
function dataOf(record: LogRecord): Record<string, unknown> {
  const data = record.data;
  if (data === undefined || data === null) {
    return {};
  }
  if (typeof data !== "object" || Array.isArray(data)) {
    throw new TypeError("session log record data is not an object");
  }
  return data as Record<string, unknown>;
}

/** Replay a log that has already been read. */
export async function replayLogText(text: string, options: ReplayOptions = {}): Promise<void> {
  const write = options.write ?? ((chunk: string) => void process.stdout.write(chunk));
  const sleep = options.sleep ?? realSleep;
  const prompt = options.prompt ?? (async () => undefined);
  const wanted = new Set(options.events ?? DEFAULT_REPLAY_EVENTS);
  const speed = Math.min(Math.max(options.speed ?? 1.0, SPEED_FLOOR), SPEED_CEILING);
  let lastTs: number | undefined;

  const lines = text.split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] as string;
    if (line.trim() === "") {
      continue;
    }
    let record: LogRecord;
    try {
      const parsed: unknown = JSON.parse(line);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        // Matching the reference, which reaches for `.get` on whatever the
        // line parsed to and raises there.
        throw new TypeError("session log line is not an object");
      }
      record = parsed as LogRecord;
    } catch (error) {
      if (error instanceof TypeError) {
        throw error;
      }
      // One corrupt line in the middle of an incident log must not end the
      // replay there.
      options.onWarning?.(`replay_log corrupt line skipped line=${index + 1}`);
      continue;
    }

    if (!wanted.has(String(record.event))) {
      continue;
    }
    const screen = dataOf(record).screen;
    // A record with no screen has nothing to draw; an empty one is a cleared
    // terminal, which is a frame.
    if (screen === undefined || screen === null) {
      continue;
    }

    const ts = typeof record.ts === "number" ? record.ts : lastTs;
    // The first frame has nothing to be late for. The guard is belt and
    // braces — without it the subtraction is NaN, which is not greater than
    // zero either — but it says why rather than relying on that.
    if (lastTs !== undefined && options.step !== true) {
      // `ts` is only undefined when `lastTs` is too, which this branch has
      // already ruled out.
      const delay = ((ts as number) - lastTs) / speed;
      // A merged or clock-adjusted log runs backwards in places, and a
      // negative delay is not something to wait out.
      if (delay > 0) {
        await sleep(delay);
      }
    }
    // Cleared first: without it a shorter frame leaves the tail of the longer
    // one behind and the replay shows something that never appeared.
    write(CLEAR_SCREEN);
    write(String(screen));
    if (options.step === true) {
      // After drawing, not before: the point of a step is to look at the
      // frame.
      await prompt(STEP_PROMPT);
    }
    lastTs = ts;
  }
}

/** Replay the log at `logPath`. */
export async function replayLog(logPath: string, options: ReplayOptions = {}): Promise<void> {
  await replayLogText(readFileSync(logPath, "utf8"), options);
}
