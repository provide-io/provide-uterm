//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Reading back what a fleet has been doing.
 *
 * Port of `provide.uterm.manager.timeseries.manager`'s reading half. A manager
 * writes one JSON line per sample; a file outlives the fleet it describes, so
 * two decisions matter:
 *
 * * **Where the current run starts.** A chart drawn over a whole file draws
 *   several runs at once. A run is taken to have restarted when the turn count
 *   drops sharply — by more than a fifth, and by at least fifty, so ordinary
 *   noise is not a restart — or when the agents go from some to none.
 * * **What a malformed line does.** Nothing. A truncated write at the end of a
 *   file, a blank line, or a line that is not an object is skipped, because a
 *   reader that failed on one bad line would lose every good one behind it.
 */

/** The share of the turn count that may vanish before it reads as a restart. */
export const EPOCH_TURN_DROP_RATIO = 0.2;

/** The fewest turns that may vanish before it reads as a restart. */
export const EPOCH_TURN_DROP_MIN = 50;

/** The most rows `recentRows` will hand back, however many are asked for. */
export const MAX_RECENT_ROWS = 5000;

/** One sample, as it was written. */
export type SampleRow = Record<string, unknown>;

/**
 * Read a count the way the reference does.
 *
 * The reference writes `int(x or 0)`; here the falsy cases need no branch of
 * their own, since absent, null and empty all become zero or a value that is
 * not a number, and both land on zero below.
 */
function count(value: unknown): number {
  const parsed = Number(value);
  // Zero rather than anything negative: a count that cannot be read must not
  // look like a fleet that has gone away, which is what a restart is.
  return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
}

/**
 * Keep only the rows belonging to the run still going.
 *
 * A restart is a sharp fall in turns, or the agents going from some to none.
 * The threshold is a share *and* a floor: without the floor a quiet fleet
 * would restart on noise, and without the share a busy one would never
 * restart at all.
 */
export function trimToLatestEpoch(rows: readonly SampleRow[]): SampleRow[] {
  if (rows.length <= 1) {
    return [...rows];
  }
  let epochStart = 0;
  let previousTurns = count((rows[0] as SampleRow).total_turns);
  let previousAgents = count((rows[0] as SampleRow).total_agents);

  for (let index = 1; index < rows.length; index += 1) {
    const row = rows[index] as SampleRow;
    const turns = count(row.total_turns);
    const agents = count(row.total_agents);
    const threshold = Math.max(EPOCH_TURN_DROP_MIN, Math.trunc(previousTurns * EPOCH_TURN_DROP_RATIO));
    const restarted = previousTurns - turns > threshold || (previousAgents > 0 && agents === 0);
    if (restarted) {
      epochStart = index;
    }
    previousTurns = turns;
    previousAgents = agents;
  }
  return rows.slice(epochStart);
}

/**
 * Read the rows out of the tail of a file's contents.
 *
 * A line that cannot be read is skipped rather than fatal: the last line of a
 * file being written is routinely half there.
 */
export function parseTail(contents: string, limit: number): SampleRow[] {
  // At least one, however small a number was asked for.
  const capped = Math.max(1, Math.trunc(limit) || 0);
  const rows: SampleRow[] = [];
  for (const raw of splitLines(contents).slice(-capped)) {
    // Trimmed and checked for emptiness, though `JSON.parse` would refuse
    // both anyway — the intent is that a blank line is not a broken row.
    const line = raw.trim();
    if (line === "") {
      continue;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      // A truncated write, or something that is not a sample at all.
      continue;
    }
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      rows.push(parsed as SampleRow);
    }
  }
  return rows;
}

/**
 * Split into lines the way Python's `splitlines` does.
 *
 * The difference that matters: a trailing line break ends the last line
 * rather than starting an empty one, so asking for the last row of a file
 * that ends in a newline gives the row and not the nothing after it.
 */
function splitLines(contents: string): string[] {
  if (contents === "") {
    // Stated, though splitting and popping would give the same answer.
    return [];
  }
  const lines = contents.split(/\r\n|\r|\n/);
  // One, not all: `"a\n\n\n"` is three lines in Python, and dropping every
  // trailing blank would change which of them the last row is.
  if (lines.at(-1) === "") {
    lines.pop();
  }
  return lines;
}

/** Where the samples are and how they are read. */
export interface TimeseriesSource {
  /** The whole file, or nothing when there is none. */
  read(): string | undefined;
}

/** What a manager says about its own timeseries. */
export interface TimeseriesInfo {
  path: string;
  interval_seconds: number;
  samples: number;
}

/** Reading a fleet's samples back. */
export class TimeseriesReader {
  readonly path: string;
  readonly intervalS: number;
  samplesCount = 0;

  readonly #source: TimeseriesSource;

  constructor(source: TimeseriesSource, options: { path?: string; intervalS?: number } = {}) {
    this.#source = source;
    this.path = options.path ?? "";
    // At least a second: a zero interval is a loop with no wait in it.
    this.intervalS = Math.max(1, Math.trunc(options.intervalS ?? 20));
  }

  /** What this manager is, for an operator asking. */
  getInfo(): TimeseriesInfo {
    return { path: this.path, interval_seconds: this.intervalS, samples: this.samplesCount };
  }

  /** The last `limit` rows, or nothing when there is no file yet. */
  readTail(limit: number): SampleRow[] {
    const contents = this.#source.read();
    if (contents === undefined) {
      // No file is not an error: a manager that has not sampled yet has
      // nothing to show, which is different from having failed.
      return [];
    }
    return parseTail(contents, limit);
  }

  /** The rows belonging to the run still going. */
  getRecent(limit = 200): SampleRow[] {
    const capped = Math.max(1, Math.min(Math.trunc(limit) || 0, MAX_RECENT_ROWS));
    return trimToLatestEpoch(this.readTail(capped));
  }
}
