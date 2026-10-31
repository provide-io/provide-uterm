//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { IBMPC_MAP, VAX42_MAP } from "./charset-tables.ts";
import { Screen, Stream } from "./index.ts";
import { CANONICAL_DECOMPOSITIONS, COMPOSITION_PAIRS } from "./normalization-tables.ts";
import { nfc } from "./normalize.ts";
import { COMBINING_CLASSES, WIDTH_RANGES } from "./wcwidth-tables.ts";

interface VtGolden {
  pyte_version: string;
  cases: Array<{
    name: string;
    cols: number;
    rows: number;
    chunks: string[];
    state: {
      display: string[];
      cursor: { x: number; y: number; hidden: boolean };
      cells: Array<{
        y: number;
        x: number;
        data: string;
        fg: string;
        bg: string;
        bold: boolean;
        italics: boolean;
        underscore: boolean;
        reverse: boolean;
        strikethrough: boolean;
        blink: boolean;
      }>;
    };
  }>;
  dec_graphics_cases: VtGolden["cases"];
  charset_tables: { U: number[]; V: number[] };
  reference_fix_cases: VtGolden["cases"];
  wcwidth_ranges: number[][];
  combining_classes: number[][];
  canonical_decompositions: number[][];
  composition_pairs: number[][];
  stall_divergences: Array<{ name: string; chunks: string[]; pyte: VtGolden["cases"][number]["state"] }>;
}

const golden = loadGolden<VtGolden>("vt_golden.json");

/** Feed `chunks` to a fresh screen and return it. */
function drive(cols: number, rows: number, chunks: readonly string[], decGraphics = false): Screen {
  const screen = new Screen(cols, rows);
  const stream = decGraphics ? new Stream(screen, { decGraphics }) : new Stream(screen);
  for (const chunk of chunks) {
    stream.feed(chunk);
  }
  return screen;
}

/** Capture the same observable state the corpus generator recorded. */
function capture(screen: Screen): VtGolden["cases"][number]["state"] {
  const cells: VtGolden["cases"][number]["state"]["cells"] = [];
  for (let y = 0; y < screen.rows; y += 1) {
    for (let x = 0; x < screen.cols; x += 1) {
      const char = screen.peek(y, x);
      if (char === undefined || screen.isDefaultChar(char)) {
        continue;
      }
      cells.push({ y, x, ...char });
    }
  }
  return {
    display: screen.display,
    cursor: { x: screen.cursor.x, y: screen.cursor.y, hidden: screen.cursor.hidden },
    cells,
  };
}

describe("Screen basics", () => {
  it("starts blank with the cursor at the origin", () => {
    const screen = new Screen(4, 2);
    expect(screen.display).toStrictEqual(["    ", "    "]);
    expect({ x: screen.cursor.x, y: screen.cursor.y }).toStrictEqual({ x: 0, y: 0 });
  });

  it("reports its own dimensions", () => {
    const screen = new Screen(7, 3);
    expect({ cols: screen.cols, rows: screen.rows }).toStrictEqual({ cols: 7, rows: 3 });
  });

  it("shows the cursor by default", () => {
    expect(new Screen(4, 2).cursor.hidden).toBe(false);
  });
});

describe("printing", () => {
  it("advances the cursor as it writes", () => {
    const screen = drive(6, 2, ["ab"]);
    expect(screen.display[0]).toBe("ab    ");
    expect(screen.cursor.x).toBe(2);
  });

  it("wraps to the next row past the last column", () => {
    const screen = drive(3, 2, ["abcd"]);
    expect(screen.display).toStrictEqual(["abc", "d  "]);
  });

  it("scrolls when wrapping off the last row", () => {
    const screen = drive(2, 2, ["abcde"]);
    expect(screen.display).toStrictEqual(["cd", "e "]);
  });
});

describe("control characters", () => {
  it("returns the cursor to column zero on carriage return", () => {
    expect(drive(6, 2, ["abc\rX"]).display[0]).toBe("Xbc   ");
  });

  it("moves down a row on line feed without changing the column", () => {
    const screen = drive(6, 3, ["abc\nd"]);
    expect(screen.display[1]).toBe("   d  ");
  });

  it("moves back one column on backspace without erasing", () => {
    expect(drive(6, 2, ["abc\bX"]).display[0]).toBe("abX   ");
  });

  it("stops at column zero on backspace", () => {
    expect(drive(6, 2, ["\bX"]).display[0]).toBe("X     ");
  });

  it("advances to the next eight-column tab stop", () => {
    expect(drive(20, 2, ["a\tb"]).cursor.x).toBe(9);
  });

  it("prints nothing for a bell", () => {
    expect(drive(6, 2, ["a\x07b"]).display[0]).toBe("ab    ");
  });
});

describe("cursor movement", () => {
  it("clamps a cursor position past the screen edge", () => {
    const screen = drive(6, 3, ["\x1b[99;99H"]);
    expect({ x: screen.cursor.x, y: screen.cursor.y }).toStrictEqual({ x: 5, y: 2 });
  });

  it("homes on a cursor position with no parameters", () => {
    const screen = drive(6, 3, ["abc\x1b[H"]);
    expect({ x: screen.cursor.x, y: screen.cursor.y }).toStrictEqual({ x: 0, y: 0 });
  });

  it("does not move above the top row", () => {
    expect(drive(6, 3, ["\x1b[5A"]).cursor.y).toBe(0);
  });

  it("restores a saved cursor", () => {
    const screen = drive(10, 4, ["\x1b[3;5H\x1b7\x1b[1;1H\x1b8"]);
    expect({ x: screen.cursor.x, y: screen.cursor.y }).toStrictEqual({ x: 4, y: 2 });
  });
});

describe("erasing", () => {
  it("erases from the cursor to the end of the line", () => {
    expect(drive(8, 2, ["abcdef\x1b[3G\x1b[K"]).display[0]).toBe("ab      ");
  });

  it("erases from the start of the line to the cursor", () => {
    expect(drive(8, 2, ["abcdef\x1b[3G\x1b[1K"]).display[0]).toBe("   def  ");
  });

  it("erases the whole screen", () => {
    expect(drive(4, 2, ["ab\ncd\x1b[2J"]).display).toStrictEqual(["    ", "    "]);
  });
});

describe("graphic rendition", () => {
  it("applies an attribute to subsequently printed cells", () => {
    const screen = drive(4, 1, ["\x1b[1ma"]);
    expect(screen.peek(0, 0)?.bold).toBe(true);
  });

  it("does not apply an attribute retroactively", () => {
    const screen = drive(4, 1, ["a\x1b[1mb"]);
    expect(screen.peek(0, 0)?.bold).toBe(false);
    expect(screen.peek(0, 1)?.bold).toBe(true);
  });

  it("resets every attribute on a zero parameter", () => {
    const screen = drive(4, 1, ["\x1b[1;31ma\x1b[0mb"]);
    expect(screen.peek(0, 1)).toMatchObject({ bold: false, fg: "default" });
  });

  it("treats an empty parameter list as a reset", () => {
    const screen = drive(4, 1, ["\x1b[1ma\x1b[mb"]);
    expect(screen.peek(0, 1)?.bold).toBe(false);
  });

  it("carries attributes across a feed boundary", () => {
    const screen = new Screen(4, 1);
    const stream = new Stream(screen);
    stream.feed("\x1b[1m");
    stream.feed("a");
    expect(screen.peek(0, 0)?.bold).toBe(true);
  });
});

describe("malformed input", () => {
  it("holds an incomplete escape sequence until it completes", () => {
    const screen = new Screen(6, 2);
    const stream = new Stream(screen);
    stream.feed("ab\x1b[");
    stream.feed("2D");
    stream.feed("x");
    expect(screen.display[0]).toBe("xb    ");
  });

  it("swallows an operating system command", () => {
    expect(drive(6, 2, ["a\x1b]0;title\x07b"]).display[0]).toBe("ab    ");
  });

  it("ignores an unknown escape", () => {
    expect(drive(6, 2, ["a\x1bZb"]).display[0]).toBe("ab    ");
  });
});

describe("Screen state the stream does not reach", () => {
  it("keeps the top of the region when only the bottom is given", () => {
    // pyte's set_margins(bottom=...) keeps the top it has; DECSTBM from the
    // stream always carries a top, so only a direct call can leave it out.
    const screen = drive(20, 6, ["\x1b[2;5r"]);
    screen.setMargins(undefined, 4);
    expect(screen.margins).toStrictEqual({ top: 1, bottom: 3 });
  });

  it("changes nothing when resized to the size it has", () => {
    const screen = drive(20, 6, ["\x1b[2;5r\x1b[3;3H"]);
    screen.resize(6, 20);
    expect(screen.margins).toStrictEqual({ top: 1, bottom: 4 });
    expect({ x: screen.cursor.x, y: screen.cursor.y }).toStrictEqual({ x: 2, y: 2 });
  });

  it("keeps its tab stops across a resize, as pyte does", () => {
    const screen = drive(10, 2, []);
    screen.resize(2, 30);
    new Stream(screen).feed("\ta\tb");
    expect(screen.display[0]?.trimEnd()).toBe(`${" ".repeat(8)}a${" ".repeat(20)}b`);
  });
});

describe("differential parity with pyte", () => {
  it("was generated from a recorded pyte version", () => {
    expect(golden.pyte_version).toMatch(/^\d+\.\d+/);
  });

  it("drops the rest of a run of text at a control, as pyte does", () => {
    // A C0 control pyte has no handler for has no width: pyte stops drawing
    // the run of text it came in, and so do the Go and C# ports. The corpus
    // keeps these under the name they had when this port drew the control.
    for (const record of golden.stall_divergences) {
      expect(capture(drive(20, 6, record.chunks))).toStrictEqual(record.pyte);
    }
    expect(golden.stall_divergences.length).toBeGreaterThan(2);
    // Only the run ends there: the next feed, or anything after a control
    // the parser acts on, draws again.
    expect(drive(20, 2, ["a\x01bc", "d"]).display[0]?.slice(0, 3)).toBe("ad ");
    expect(drive(20, 2, ["a\x85b\rc"]).display[0]?.slice(0, 3)).toBe("c  ");
  });

  it("still ignores the two controls the Go port ignores", () => {
    expect(drive(20, 2, ["a\x00b"]).display[0]?.slice(0, 3)).toBe("ab ");
    expect(drive(20, 2, ["a\x7fb"]).display[0]?.slice(0, 3)).toBe("ab ");
  });

  it("matches the full observable state of every case", () => {
    const mismatches: string[] = [];
    for (const testCase of golden.cases) {
      const actual = capture(drive(testCase.cols, testCase.rows, testCase.chunks));
      if (JSON.stringify(actual) !== JSON.stringify(testCase.state)) {
        mismatches.push(testCase.name);
      }
    }
    expect(mismatches).toStrictEqual([]);
    expect(golden.cases.length).toBeGreaterThan(70);
  });
});

describe("character width", () => {
  it("measures and composes with the reference's tables", () => {
    // Generated by scripts/gen_unicode_tables.py; a mismatch means the corpus
    // was regenerated from a newer wcwidth or unicodedata2 and that should be
    // rerun.
    expect(WIDTH_RANGES).toStrictEqual(golden.wcwidth_ranges.flat());
    expect(COMBINING_CLASSES).toStrictEqual(golden.combining_classes.flat());
    expect(CANONICAL_DECOMPOSITIONS).toStrictEqual(golden.canonical_decompositions.flat());
    expect(COMPOSITION_PAIRS).toStrictEqual(golden.composition_pairs.flat());
  });

  it("composes every primary composite, Hangul and blocked marks as the reference does", () => {
    for (const [first, second, composite] of golden.composition_pairs) {
      expect(nfc(String.fromCodePoint(first as number, second as number))).toBe(
        String.fromCodePoint(composite as number),
      );
    }
    // Hangul, composed algorithmically: L + V, then LV + T.
    expect(nfc("\u1100\u1161\u11a8")).toBe("\uac01");
    expect(nfc("\uac00\u11a8")).toBe("\uac01");
    expect(nfc("\uac01")).toBe("\uac01");
    // A singleton decomposes and stays decomposed; a blocked mark stays put.
    expect(nfc("\u212b")).toBe("\u00c5");
    expect(nfc("a\u0301\u0301")).toBe("\u00e1\u0301");
    expect(nfc("\u0301a")).toBe("\u0301a");
    expect(nfc("\u{105d2}\u0307")).toBe("\u{105c9}");
  });

  it("matches the emulator's fixes for what plain pyte raises on", () => {
    // Recorded through the screen the Python emulator runs, not plain pyte:
    // half of an overwritten wide character, and VPA in origin mode with no
    // scrolling region. All four ports leave this state.
    const mismatches: string[] = [];
    for (const testCase of golden.reference_fix_cases) {
      const actual = capture(drive(testCase.cols, testCase.rows, testCase.chunks));
      if (JSON.stringify(actual) !== JSON.stringify(testCase.state)) {
        mismatches.push(testCase.name);
      }
    }
    expect(mismatches).toStrictEqual([]);
    expect(golden.reference_fix_cases.length).toBeGreaterThan(3);
  });
});

describe("DEC special graphics", () => {
  it("matches pyte with use_utf8 off for every case", () => {
    const mismatches: string[] = [];
    for (const testCase of golden.dec_graphics_cases) {
      const actual = capture(drive(testCase.cols, testCase.rows, testCase.chunks, true));
      if (JSON.stringify(actual) !== JSON.stringify(testCase.state)) {
        mismatches.push(testCase.name);
      }
    }
    expect(mismatches).toStrictEqual([]);
    expect(golden.dec_graphics_cases.length).toBeGreaterThan(10);
  });

  it("is off by default: designations are consumed, and SO and SI ignored", () => {
    const screen = drive(20, 2, ["\x1b(0q\x1b)U\x1b(Bq\x0eq\x0fb"]);
    expect(screen.display[0]?.slice(0, 5)).toBe("qqqb ");
    expect(screen.charsets).toStrictEqual({ g0: "ascii", g1: "dec", shifted: false });
    expect(screen.charsetsDesignated).toBe(false);
  });

  it("draws from the reference's IBM codepage 437 and VAX42 tables", () => {
    expect(IBMPC_MAP).toStrictEqual(golden.charset_tables.U);
    expect(VAX42_MAP).toStrictEqual(golden.charset_tables.V);
    expect(IBMPC_MAP).toHaveLength(256);
    expect(VAX42_MAP).toHaveLength(256);
  });

  it("designates codepage 437 and VAX42 into either slot", () => {
    const screen = drive(20, 2, ["\x1b(U\x1b)V"], true);
    expect(screen.charsets).toStrictEqual({ g0: "ibmpc", g1: "vax42", shifted: false });
    expect(screen.charsetsDesignated).toBe(true);
  });

  it("returns to ASCII on a full reset in the stream", () => {
    const screen = drive(20, 2, ["\x1b(U\x1b)0\x0e\x1bc"], true);
    expect(screen.charsets).toStrictEqual({ g0: "ascii", g1: "dec", shifted: false });
    expect(screen.charsetsDesignated).toBe(false);
  });

  it("returns to ASCII when the screen is reset", () => {
    const screen = new Screen(20, 2);
    const stream = new Stream(screen, { decGraphics: true });
    stream.feed("\x1b(0\x1b)0\x0e");
    screen.reset();
    stream.feed("q");
    expect(screen.display[0]?.slice(0, 2)).toBe("q ");
  });
});
