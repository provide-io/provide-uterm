//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type ColorMode,
  type PixelReader,
  type Rgb,
  type Rgba,
  renderFrame,
  SGR_EMITTERS,
  sgr16,
  sgr256,
  sgrTruecolor,
} from "./index.ts";

interface ImageGolden {
  modes: string[];
  sgr: Array<{ name: string; fg: number[]; bg: number[]; truecolor: string; "256": string; "16": string }>;
  frames: Array<{ name: string; rows: number[][][]; width: number; height: number; truecolor: string; "16": string }>;
}

const golden = loadGolden<ImageGolden>("imagerender_golden.json");

/** Read a recorded image, row by row. */
function reader(rows: number[][][]): PixelReader {
  return (x, y) => (rows[y] as number[][])[x] as unknown as Rgba;
}

describe("emitting a colour pair", () => {
  it.each(golden.sgr)("$name", (record) => {
    const fg = record.fg as unknown as Rgb;
    const bg = record.bg as unknown as Rgb;
    expect(sgrTruecolor(fg, bg)).toBe(record.truecolor);
    expect(sgr256(fg, bg)).toBe(record["256"]);
    expect(sgr16(fg, bg)).toBe(record["16"]);
  });

  it("offers an emitter for every mode", () => {
    expect(Object.keys(SGR_EMITTERS).sort()).toEqual(golden.modes);
  });

  it("puts the foreground before the background", () => {
    // A terminal reading them the other way round renders the image
    // inverted, which is the kind of wrong that looks deliberate.
    expect(sgrTruecolor([1, 2, 3], [4, 5, 6])).toBe("\x1b[38;2;1;2;3;48;2;4;5;6m");
  });

  it("takes each code from the colour it applies to", () => {
    // The foreground code from the foreground's match and the background code
    // from the background's — a quantiser returns both for one colour, and
    // taking the wrong one paints a cell in a colour neither pixel had.
    const emitted = sgr16([255, 0, 0], [0, 0, 255]);
    expect(emitted).toBe(golden.sgr.find((entry) => entry.name === "red on blue")?.["16"]);
  });
});

describe("rendering a frame", () => {
  it.each(golden.frames)("$name", (record) => {
    const pixels = reader(record.rows);
    expect(renderFrame(pixels, record.width, record.height, sgrTruecolor)).toBe(record.truecolor);
    expect(renderFrame(pixels, record.width, record.height, sgr16)).toBe(record["16"]);
  });

  it("homes the cursor so a frame overwrites the last", () => {
    // Otherwise every frame scrolls the one before it off the screen.
    const rows = [[[255, 0, 0, 255]], [[0, 0, 255, 255]]];
    expect(renderFrame(reader(rows), 1, 2, sgr16).startsWith("\x1b[H")).toBe(true);
  });

  it("draws two pixel rows per terminal row", () => {
    const rows = [[[255, 0, 0, 255]], [[0, 255, 0, 255]], [[0, 0, 255, 255]], [[255, 255, 0, 255]]];
    const frame = renderFrame(reader(rows), 1, 4, sgr16);
    expect(frame.split("\r\n").filter((line) => line !== "")).toHaveLength(2);
  });

  it("emits no escape for a repeated colour", () => {
    // A run of identical pixels costs one escape and then nothing, which is
    // the difference between a frame that fits in a terminal's buffer and one
    // that does not.
    const run = [Array.from({ length: 4 }, () => [255, 0, 0, 255]), Array.from({ length: 4 }, () => [0, 0, 255, 255])];
    const frame = renderFrame(reader(run), 4, 2, sgr16);
    expect(frame.match(/\x1b\[\d+;\d+m/g)).toHaveLength(1);
    expect(frame).toContain("▄▄▄▄");
  });

  it("emits again when a run is broken", () => {
    const rows = [
      [
        [255, 0, 0, 255],
        [255, 0, 0, 255],
        [0, 255, 0, 255],
        [255, 0, 0, 255],
      ],
      Array.from({ length: 4 }, () => [0, 0, 255, 255]),
    ];
    expect(renderFrame(reader(rows), 4, 2, sgr16).match(/\x1b\[\d+;\d+m/g)).toHaveLength(3);
  });

  it("starts each row afresh", () => {
    // The comparison is against the last sequence written on that row, so a
    // row opening with the colour the previous one ended in still says so.
    const rows = [[[255, 0, 0, 255]], [[0, 0, 255, 255]], [[255, 0, 0, 255]], [[0, 0, 255, 255]]];
    const frame = renderFrame(reader(rows), 1, 4, sgr16);
    expect(frame.match(/\x1b\[\d+;\d+m/g)).toHaveLength(2);
  });

  it("resets at the end of every row", () => {
    // So a colour cannot leak into whatever a terminal draws next.
    const rows = [[[255, 0, 0, 255]], [[0, 0, 255, 255]]];
    expect(renderFrame(reader(rows), 1, 2, sgr16).endsWith("\x1b[0m\r\n")).toBe(true);
  });

  it("pairs a last unpaired row with black", () => {
    // Rather than dropping it or reading past the end of the image.
    const odd = golden.frames.find((entry) => entry.name === "an odd number of pixel rows");
    const pixels = reader(odd?.rows as number[][][]);
    expect(renderFrame(pixels, odd?.width as number, odd?.height as number, sgr16)).toBe(odd?.["16"]);
  });

  it("draws a transparent pixel as black rather than skipping it", () => {
    // There is no way to punch a hole in a terminal cell.
    const transparent = golden.frames.find((entry) => entry.name === "a transparent top pixel");
    const pixels = reader(transparent?.rows as number[][][]);
    expect(renderFrame(pixels, 1, 2, sgr16)).toBe(transparent?.["16"]);
  });

  it("treats half opacity as opaque and one below it as not", () => {
    // The boundary is inclusive on the opaque side, which is only visible
    // either side of it.
    const half = golden.frames.find((entry) => entry.name === "half opacity exactly");
    const under = golden.frames.find((entry) => entry.name === "one under half opacity");
    expect(renderFrame(reader(half?.rows as number[][][]), 1, 2, sgr16)).toBe(half?.["16"]);
    expect(renderFrame(reader(under?.rows as number[][][]), 1, 2, sgr16)).toBe(under?.["16"]);
    expect(half?.["16"]).not.toBe(under?.["16"]);
  });

  it("renders an empty image as a cursor home and nothing else", () => {
    expect(renderFrame(() => [0, 0, 0, 0], 0, 0, sgr16)).toBe("\x1b[H");
  });

  it("renders through every mode", () => {
    const rows = [[[255, 0, 0, 255]], [[0, 0, 255, 255]]];
    for (const mode of golden.modes) {
      const emit = SGR_EMITTERS[mode as ColorMode];
      expect(renderFrame(reader(rows), 1, 2, emit)).toContain("▄");
    }
  });
});
