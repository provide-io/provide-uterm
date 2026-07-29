//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  EPOCH_TURN_DROP_MIN,
  EPOCH_TURN_DROP_RATIO,
  MAX_RECENT_ROWS,
  parseTail,
  type SampleRow,
  TimeseriesReader,
  trimToLatestEpoch,
} from "./index.ts";

interface TimeseriesGolden {
  constants: { epoch_turn_drop_ratio: number; epoch_turn_drop_min: number };
  epochs: Array<{ name: string; rows: SampleRow[]; trimmed: SampleRow[] }>;
  tails: Array<{ name: string; contents: string; limit: number; rows: SampleRow[] }>;
  missing_file: SampleRow[];
}

const golden = loadGolden<TimeseriesGolden>("timeseries_golden.json");

/** A sample with the two counts the epoch heuristic reads. */
function row(turns: number, agents: number): SampleRow {
  return { total_turns: turns, total_agents: agents, tag: "" };
}

describe("where the current run starts", () => {
  it.each(golden.epochs)("$name", (record) => {
    expect(trimToLatestEpoch(record.rows)).toEqual(record.trimmed);
  });

  it("uses the thresholds the reference uses", () => {
    expect(EPOCH_TURN_DROP_RATIO).toBe(golden.constants.epoch_turn_drop_ratio);
    expect(EPOCH_TURN_DROP_MIN).toBe(golden.constants.epoch_turn_drop_min);
  });

  it("keeps a run that only grew", () => {
    const rows = [row(10, 1), row(200, 4), row(500, 8)];
    expect(trimToLatestEpoch(rows)).toEqual(rows);
  });

  it("needs both the share and the floor before it calls it a restart", () => {
    // Without the floor a quiet fleet restarts on noise; without the share a
    // busy one never restarts at all.
    //
    // A quiet fleet: half its turns gone, but only 50 of them.
    expect(trimToLatestEpoch([row(100, 2), row(50, 2)])).toHaveLength(2);
    expect(trimToLatestEpoch([row(100, 2), row(49, 2)])).toHaveLength(1);
    // A busy one: 200 turns gone, which is a fifth exactly and so not enough.
    expect(trimToLatestEpoch([row(1000, 4), row(800, 4)])).toHaveLength(2);
    expect(trimToLatestEpoch([row(1000, 4), row(799, 4)])).toHaveLength(1);
  });

  it("treats the agents going away as a restart", () => {
    expect(trimToLatestEpoch([row(100, 4), row(110, 0)])).toEqual([row(110, 0)]);
  });

  it("does not treat an idle fleet as restarting every sample", () => {
    // Nought to nought is not a fleet going away.
    const rows = [row(100, 0), row(110, 0), row(120, 0)];
    expect(trimToLatestEpoch(rows)).toEqual(rows);
  });

  it("does not treat agents arriving as a restart", () => {
    const rows = [row(100, 0), row(110, 4)];
    expect(trimToLatestEpoch(rows)).toEqual(rows);
  });

  it("keeps only the run after the last restart", () => {
    const rows = [row(1000, 4), row(5, 1), row(900, 4), row(3, 1), row(50, 2)];
    expect(trimToLatestEpoch(rows)).toEqual([row(3, 1), row(50, 2)]);
  });

  it("hands back what it was given when there is nothing to trim", () => {
    expect(trimToLatestEpoch([])).toEqual([]);
    expect(trimToLatestEpoch([row(10, 2)])).toEqual([row(10, 2)]);
  });

  it("does not change what it was given", () => {
    // A caller may still want the whole file.
    const rows = [row(1000, 4), row(5, 1)];
    trimToLatestEpoch(rows);
    expect(rows).toHaveLength(2);
  });

  it("reads a count that is missing, null or text the way the reference does", () => {
    expect(trimToLatestEpoch([{}, row(10, 2)])).toEqual([{}, row(10, 2)]);
    expect(trimToLatestEpoch([{ total_turns: null, total_agents: null }, row(10, 2)])).toHaveLength(2);
    // Text is read as the number it spells, so a restart is still seen.
    expect(trimToLatestEpoch([{ total_turns: "1000", total_agents: "4" }, row(10, 1)])).toHaveLength(1);
  });

  it("does not let an unreadable count look like a fleet going away", () => {
    // Zero, not something negative: a count nobody can read is not a restart.
    expect(trimToLatestEpoch([row(100, 4), { total_turns: 110, total_agents: "nonsense" }])).toEqual([
      { total_turns: 110, total_agents: "nonsense" },
    ]);
  });

  it("reads a fractional count as the whole number below it", () => {
    // As the reference's `int()` does; keeping the fraction moves the
    // threshold and changes where a run is said to start.
    expect(trimToLatestEpoch([row(100.9, 2), { total_turns: 50.9, total_agents: 2 }])).toHaveLength(2);
    expect(trimToLatestEpoch([row(100.9, 2), { total_turns: 50.4, total_agents: 2 }])).toHaveLength(2);
    expect(trimToLatestEpoch([{ total_turns: 249.9, total_agents: 2 }, row(199, 2)])).toHaveLength(2);
  });

  it("reads a count that is not a number at all as nothing", () => {
    // Rather than as a restart every time, which would trim a whole file to
    // its last row.
    const rows = [{ total_turns: "nonsense", total_agents: {} }, row(10, 2)];
    expect(trimToLatestEpoch(rows)).toEqual(rows);
  });
});

describe("reading the tail of a file", () => {
  it.each(golden.tails)("$name", (record) => {
    expect(parseTail(record.contents, record.limit)).toEqual(record.rows);
  });

  it("skips a line it cannot read rather than losing the rest", () => {
    // The last line of a file being written is routinely half there.
    const contents = '{"a":1}\n{"a":2\n{"a":3}\n';
    expect(parseTail(contents, 10)).toEqual([{ a: 1 }, { a: 3 }]);
  });

  it("skips a line that is not a sample", () => {
    // A list or a bare number is not a row, and reading it as one would put
    // something with no fields into a chart.
    const contents = '{"a":1}\n[1,2]\n42\n"hello"\nnull\ntrue\n{"a":2}\n';
    expect(parseTail(contents, 10)).toEqual([{ a: 1 }, { a: 2 }]);
  });

  it("skips blank lines and lines of spaces", () => {
    expect(parseTail('{"a":1}\n\n   \n\t\n{"a":2}\n', 10)).toEqual([{ a: 1 }, { a: 2 }]);
  });

  it("reads a file with no trailing newline", () => {
    expect(parseTail('{"a":1}\n{"a":2}', 10)).toEqual([{ a: 1 }, { a: 2 }]);
  });

  it("gives back the last rows, not the first", () => {
    const contents = Array.from({ length: 10 }, (_, index) => `{"a":${index}}`).join("\n");
    expect(parseTail(contents, 3)).toEqual([{ a: 7 }, { a: 8 }, { a: 9 }]);
  });

  it("reads at least one row however small a limit it is given", () => {
    for (const limit of [0, -5, Number.NaN]) {
      expect(parseTail('{"a":1}\n{"a":2}\n', limit)).toEqual([{ a: 2 }]);
    }
  });

  it("reads text outside ASCII", () => {
    expect(parseTail('{"a":"héllo ☃"}\n', 10)).toEqual([{ a: "héllo ☃" }]);
  });

  it("counts a run of trailing newlines the way Python counts it", () => {
    // `"a\n\n\n"` is three lines, the last two blank, so the last row is
    // blank — dropping every trailing blank would give a different answer.
    expect(parseTail('{"a":1}\n\n\n', 1)).toEqual([]);
    expect(parseTail('{"a":1}\n\n\n', 3)).toEqual([{ a: 1 }]);
  });

  it("reads a file written with carriage returns", () => {
    // An old client, or a file that crossed a platform.
    expect(parseTail('{"a":1}\r\n{"a":2}\r\n', 10)).toEqual([{ a: 1 }, { a: 2 }]);
    expect(parseTail('{"a":1}\r{"a":2}\r', 10)).toEqual([{ a: 1 }, { a: 2 }]);
  });

  it("reads nothing out of nothing", () => {
    expect(parseTail("", 10)).toEqual([]);
    expect(parseTail("\n\n\n", 10)).toEqual([]);
  });
});

describe("a reader over a manager's file", () => {
  /** A reader over some contents, or over no file at all. */
  function readerOf(contents: string | undefined, options?: { path?: string; intervalS?: number }) {
    return new TimeseriesReader({ read: () => contents }, options ?? {});
  }

  it("says nothing when there is no file yet", () => {
    // A manager that has not sampled yet has nothing to show, which is not
    // the same as having failed.
    expect(readerOf(undefined).readTail(10)).toEqual(golden.missing_file);
    expect(readerOf(undefined).getRecent()).toEqual([]);
  });

  it("reads the rows out of the file it was given", () => {
    expect(readerOf('{"a":1}\n{"a":2}\n').readTail(10)).toEqual([{ a: 1 }, { a: 2 }]);
  });

  it("trims what it hands back to the run still going", () => {
    const contents = [row(1000, 4), row(5, 1), row(10, 1)].map((sample) => JSON.stringify(sample)).join("\n");
    expect(readerOf(contents).getRecent()).toEqual([row(5, 1), row(10, 1)]);
  });

  it("will not be asked for more than it will give", () => {
    // A caller asking for everything gets a bounded answer rather than a file
    // read into memory.
    const contents = Array.from({ length: MAX_RECENT_ROWS + 1 }, (_, index) => `{"a":${index}}`).join("\n");
    expect(readerOf(contents).getRecent(1_000_000)).toHaveLength(MAX_RECENT_ROWS);
    expect(MAX_RECENT_ROWS).toBe(5000);
  });

  it("gives at least one row for a limit that asks for none", () => {
    const contents = '{"a":1}\n{"a":2}\n';
    for (const limit of [0, -1, Number.NaN]) {
      expect(readerOf(contents).getRecent(limit)).toEqual([{ a: 2 }]);
    }
  });

  it("describes itself for an operator asking", () => {
    const reader = readerOf("", { path: "/logs/metrics/swarm.jsonl", intervalS: 30 });
    expect(reader.getInfo()).toEqual({
      path: "/logs/metrics/swarm.jsonl",
      interval_seconds: 30,
      samples: 0,
    });
  });

  it("counts an interval of at least a second", () => {
    // A zero interval is a loop with no wait in it.
    for (const given of [0, -5, 0.4]) {
      expect(readerOf("", { intervalS: given }).intervalS).toBe(1);
    }
    expect(readerOf("", { intervalS: 45 }).intervalS).toBe(45);
    // And the reference's own default when nobody says.
    expect(readerOf("").intervalS).toBe(20);
  });

  it("reports the samples it has been told about", () => {
    const reader = readerOf("");
    expect(reader.getInfo().samples).toBe(0);
    reader.samplesCount = 7;
    expect(reader.getInfo().samples).toBe(7);
  });
});
