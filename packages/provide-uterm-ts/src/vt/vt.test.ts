//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { Screen, Stream } from "./index.ts";

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
  stall_divergences: Array<{ name: string; chunks: string[]; pyte: { cursor: { x: number; y: number } } }>;
}

const golden = loadGolden<VtGolden>("vt_golden.json");

/** Feed `chunks` to a fresh screen and return it. */
function drive(cols: number, rows: number, chunks: readonly string[]): Screen {
  const screen = new Screen(cols, rows);
  const stream = new Stream(screen);
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

describe("differential parity with pyte", () => {
  it("was generated from a recorded pyte version", () => {
    expect(golden.pyte_version).toMatch(/^\d+\.\d+/);
  });

  it("keeps parsing after a control pyte stalls on", () => {
    // pyte has no handler for most C0 controls and its parser stalls: every
    // byte after the control is swallowed for the life of the stream, so one
    // stray byte would freeze the display permanently. The Go port draws such
    // a control instead, and this port follows Go — the ports are meant to
    // agree with each other, and a stall is a bug rather than a contract.
    for (const record of golden.stall_divergences) {
      const pyteCursorX = record.pyte.cursor.x;
      const ours = drive(20, 6, record.chunks);
      expect(ours.cursor.x).toBeGreaterThan(pyteCursorX);
    }
    expect(golden.stall_divergences.length).toBeGreaterThan(2);
    // The text after the control survives, which is the point.
    expect(drive(20, 2, ["a\x01bc"]).display[0]?.slice(0, 4)).toBe("a\x01bc");
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
