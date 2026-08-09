//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden, must } from "../testing/golden.ts";
import { ANSI16_PALETTE, nearest16, nearest256, type Rgb, XTERM256_PALETTE } from "./index.ts";

interface PaletteGolden {
  ansi16: number[][];
  xterm256: number[][];
  cases: Array<{ name: string; rgb: number[]; ansi16: number[]; xterm256: number }>;
  round_trip_256: number[];
  round_trip_16: number[][];
  sweep: Array<{ rgb: number[]; ansi16: number[]; xterm256: number }>;
}

const golden = loadGolden<PaletteGolden>("palette_golden.json");

describe("the palettes themselves", () => {
  it("has the sixteen standard colours with their codes", () => {
    expect(ANSI16_PALETTE.map((entry) => [entry.r, entry.g, entry.b, entry.fg, entry.bg])).toEqual(golden.ansi16);
  });

  it("builds the xterm palette entry for entry", () => {
    // The cube's first step is 55 wide and the rest are 40, so a uniform step
    // is right for two of the six levels and wrong for four.
    expect(XTERM256_PALETTE.map((entry) => [...entry])).toEqual(golden.xterm256);
  });

  it("has two hundred and fifty six entries", () => {
    expect(XTERM256_PALETTE).toHaveLength(256);
    expect(ANSI16_PALETTE).toHaveLength(16);
  });

  it("starts with the standard sixteen", () => {
    for (let index = 0; index < 16; index += 1) {
      const entry = ANSI16_PALETTE[index] as { r: number; g: number; b: number };
      expect([...(XTERM256_PALETTE[index] as Rgb)]).toEqual([entry.r, entry.g, entry.b]);
    }
  });
});

describe("mapping a colour onto a palette", () => {
  it.each(golden.cases)("$name", (record) => {
    const [r, g, b] = record.rgb as [number, number, number];
    expect([...nearest16(r, g, b)]).toEqual(record.ansi16);
    expect(nearest256(r, g, b)).toBe(record.xterm256);
  });

  it("maps every swept colour as the reference does", () => {
    // A coarse sweep of the whole cube, which is what catches a wrong
    // distance metric rather than a wrong table.
    for (const record of golden.sweep) {
      const [r, g, b] = record.rgb as [number, number, number];
      expect([...nearest16(r, g, b)]).toEqual(record.ansi16);
      expect(nearest256(r, g, b)).toBe(record.xterm256);
    }
  });

  it("maps every palette entry back as the reference does", () => {
    expect(XTERM256_PALETTE.map((entry) => nearest256(...(entry as [number, number, number])))).toEqual(
      golden.round_trip_256,
    );
    expect(ANSI16_PALETTE.map((entry) => [...nearest16(entry.r, entry.g, entry.b)])).toEqual(golden.round_trip_16);
  });

  it("gives the first of two equally near colours", () => {
    // The palette repeats two colours the standard sixteen already have, so
    // the tie-break is reachable rather than theoretical: index 16 is black,
    // which index 0 already was, and 231 is white, which 15 already was.
    expect(nearest256(0, 0, 0)).toBe(0);
    expect(nearest256(255, 255, 255)).toBe(15);
    expect(golden.round_trip_256[16]).toBe(0);
    expect(golden.round_trip_256[231]).toBe(15);
  });

  it("finds an exact colour exactly", () => {
    expect(nearest256(255, 0, 0)).toBe(196);
    expect([...nearest16(170, 0, 0)]).toEqual([31, 41]);
  });

  it("measures distance in all three channels", () => {
    // A metric that ignored one would map these to the same entry.
    expect(nearest256(255, 0, 0)).not.toBe(nearest256(0, 255, 0));
    expect(nearest256(0, 255, 0)).not.toBe(nearest256(0, 0, 255));
  });

  it("takes a colour outside the range without complaint", () => {
    // A renderer upstream may have computed one; refusing here would lose a
    // whole frame over one pixel.
    const outside = golden.cases.find((entry) => entry.name === "a colour outside the range");
    const [r, g, b] = must(outside, "the out-of-range colour case").rgb as [number, number, number];
    expect(nearest256(r, g, b)).toBe(outside?.xterm256);
  });
});
