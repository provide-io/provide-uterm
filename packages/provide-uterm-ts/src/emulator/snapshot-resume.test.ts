//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A snapshot must leave a viewer's terminal where the live stream expects it.
 *
 * Port of `test_emulator_snapshot_resume.py`. A viewer is painted from
 * `ansiScreen()` and then fed the rest of the stream, which assumes the cursor,
 * pen and modes the bytes before it left. So: snapshot at a split point, paint
 * a fresh emulator with it, feed the remainder, and the result must equal the
 * uninterrupted stream's -- for the relative-movement fixture every port
 * replays, at every token boundary.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { penSgr, renderCellRows } from "../render/index.ts";
import { encodeCp437 } from "../screen/index.ts";
import { CONFORMANCE_DIR } from "../testing/golden.ts";
import { type Char, defaultChar, Screen, Stream } from "../vt/index.ts";
import { TerminalEmulator } from "./index.ts";

const FIXTURE = readFileSync(join(CONFORMANCE_DIR, "snapshot", "relative_paint_80x24.bin"));

/**
 * One terminal token: a CSI sequence, a charset designation, any other escape
 * or a single byte. Splits fall between tokens: a viewer handed half an escape
 * sequence would print the other half whatever the snapshot said.
 */
const TOKEN = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[\s\S]|[\s\S]/g;

function splitPoints(data: Uint8Array): number[] {
  const text = Buffer.from(data).toString("latin1");
  return [...text.matchAll(TOKEN)].map((match) => (match.index as number) + match[0].length);
}

type Encoding = "utf-8" | "cp437";

function emulator(encoding: Encoding, cols: number, rows: number): TerminalEmulator {
  return new TerminalEmulator({ cols, rows, receiveEncoding: encoding });
}

/** What the browser element writes for a snapshot frame's screen. */
function paint(snapshot: string, encoding: Encoding): Uint8Array {
  const text = snapshot.replace(/\n/g, "\r\n");
  if (encoding === "utf-8") {
    return new Uint8Array(Buffer.from(text, "utf-8"));
  }
  // Every character a CP437 session painted came from a CP437 byte, so the
  // round trip goes back through the same table.
  return encodeCp437(text);
}

function state(subject: TerminalEmulator): [string, { x: number; y: number }, string] {
  const snapshot = subject.getSnapshot();
  return [snapshot.screen, snapshot.cursor, subject.ansiScreen()];
}

function straight(data: Uint8Array, encoding: Encoding, cols: number, rows: number): TerminalEmulator {
  const subject = emulator(encoding, cols, rows);
  subject.process(data);
  return subject;
}

function resumed(data: Uint8Array, split: number, encoding: Encoding, cols: number, rows: number): TerminalEmulator {
  const live = emulator(encoding, cols, rows);
  live.process(data.subarray(0, split));
  const viewer = emulator(encoding, cols, rows);
  viewer.process(paint(live.ansiScreen(), encoding));
  viewer.process(data.subarray(split));
  return viewer;
}

function expectResumes(data: Uint8Array, split: number, encoding: Encoding = "utf-8", cols = 80, rows = 24): void {
  expect(state(resumed(data, split, encoding, cols, rows)), `split at byte ${split} (${encoding})`).toStrictEqual(
    state(straight(data, encoding, cols, rows)),
  );
}

function bytes(text: string): Uint8Array {
  return new Uint8Array(Buffer.from(text, "latin1"));
}

/** The state that follows the painted rows. */
function suffix(subject: TerminalEmulator): string {
  return subject.ansiScreen().split("\n").at(-1) as string;
}

describe("the relative-movement fixture every port replays", () => {
  it("is the committed 80x24 stream", () => {
    expect(FIXTURE.length).toBe(4634);
  });

  // Every third token boundary plus the start: hundreds of distinct states,
  // kept to a few seconds even under coverage instrumentation. (Every
  // boundary passes; it takes several times as long.)
  it.each<Encoding>(["utf-8", "cp437"])(
    "resumes identically at every third token boundary (%s)",
    (encoding) => {
      const points = splitPoints(FIXTURE).filter((_, index) => index % 3 === 0);
      expect(points.length).toBeGreaterThan(500);
      for (const split of [0, ...points]) {
        expectResumes(FIXTURE, split, encoding);
      }
    },
    60_000,
  );
});

describe("cursor and pen", () => {
  it("leaves the cursor where the program left it", () => {
    const stream = bytes("top\r\n\x1b[2Bmid\x1b[3D");
    const live = straight(stream, "utf-8", 20, 5);
    expect(suffix(live)).toContain("\x1b[4;1H");
    expectResumes(bytes("top\r\n\x1b[2Bmid\x1b[3D\x1b[1Aup"), stream.length, "utf-8", 20, 5);
  });

  it("keeps a hidden cursor hidden and shows a visible one", () => {
    expect(suffix(straight(bytes("\x1b[?25l"), "utf-8", 20, 5)).endsWith("\x1b[?25l")).toBe(true);
    expect(suffix(straight(bytes("\x1b[?25l\x1b[?25h"), "utf-8", 20, 5)).endsWith("\x1b[?25h")).toBe(true);
  });

  it("restores every pen attribute and both colours", () => {
    const live = straight(bytes("\x1b[1;3;4;5;7;9;38;5;208;48;2;1;2;3mX"), "utf-8", 20, 5);
    expect(suffix(live)).toContain("\x1b[0;1;3;4;5;7;9;38;2;255;135;0;48;2;1;2;3m");
  });

  it("encodes a pen whose colours were never set as a bare reset", () => {
    expect(penSgr({ ...defaultChar(), fg: "", bg: "" })).toBe("\x1b[0m");
    expect(penSgr({ ...defaultChar(), fg: "brown", bg: "brightbrown" })).toBe("\x1b[0;33;103m");
  });

  it("draws in the live pen after resuming", () => {
    const before = bytes("\x1b[44;1;37m\x1b[5C");
    expectResumes(bytes("\x1b[44;1;37m\x1b[5C\x1b[1Bboard"), before.length, "utf-8", 20, 5);
  });
});

describe("scroll region", () => {
  it("is restored, and a whole-screen region is sent as none", () => {
    expect(suffix(straight(bytes("\x1b[3;7r\x1b[2;4H"), "utf-8", 20, 10))).toContain("\x1b[3;7r\x1b[?6l");
    expect(suffix(straight(bytes("\x1b[1;10r"), "utf-8", 20, 10))).toContain("\x1b[r\x1b[?6l");
  });

  it("scrolls the same after resuming inside it", () => {
    const stream = bytes("line1\r\n\x1b[2;5r\x1b[3Bin\r\n\r\n\r\nscrolled\r\nagain");
    for (const split of splitPoints(stream)) {
      expectResumes(stream, split, "utf-8", 20, 8);
    }
  });
});

describe("the deferred wrap at the last column", () => {
  it("survives, the last cell redrawn in its own style", () => {
    const stream = bytes(`\x1b[2;1H${"x".repeat(19)}\x1b[32mY\x1b[35mwrapped`);
    const live = straight(bytes(`\x1b[2;1H${"x".repeat(19)}\x1b[32mY`), "utf-8", 20, 4);
    expect(live.getSnapshot().cursor).toStrictEqual({ x: 20, y: 1 });
    expect(suffix(live)).toContain("\x1b[2;20H\x1b[0;32mY");
    for (const split of splitPoints(stream)) {
      expectResumes(stream, split, "utf-8", 20, 4);
    }
  });

  it("survives the row under it scrolling away", () => {
    const stream = bytes(`\x1b[3;1H${"y".repeat(20)}\x1bDnext`);
    const live = straight(stream.subarray(0, stream.length - 4), "utf-8", 20, 3);
    expect(live.getSnapshot().cursor).toStrictEqual({ x: 20, y: 2 });
    expect(suffix(live)).toContain("\x1b[3;20H\x1b[0m ");
    expectResumes(stream, stream.length - 4, "utf-8", 20, 3);
  });
});

describe("wide characters", () => {
  // conformance/snapshot/wide_resume.json: each stream, and the snapshot the
  // Python emulator takes of it in a UTF-8 session. Every port writes those
  // bytes, and resumes from a snapshot at every point exactly.
  const corpus = JSON.parse(readFileSync(join(CONFORMANCE_DIR, "snapshot", "wide_resume.json"), "utf-8")) as {
    records: Array<{ name: string; cols: number; rows: number; stream: string; ansi_screen: string }>;
  };
  const utf8 = (text: string): Uint8Array => new Uint8Array(Buffer.from(text, "utf-8"));
  /** Token ends over characters: a snapshot is never taken inside one. */
  const characterSplits = (text: string): number[] =>
    [...text.matchAll(/\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[\s\S]|[\s\S]/gu)].map(
      (match) => (match.index as number) + match[0].length,
    );

  it("has the records every port replays", () => {
    expect(corpus.records.length).toBeGreaterThan(15);
  });

  for (const record of corpus.records) {
    it(`writes the recorded snapshot: ${record.name}`, () => {
      expect(straight(utf8(record.stream), "utf-8", record.cols, record.rows).ansiScreen()).toBe(record.ansi_screen);
    });

    it(`resumes exactly from a snapshot at every point: ${record.name}`, () => {
      const whole = state(straight(utf8(record.stream), "utf-8", record.cols, record.rows));
      for (const split of [0, ...characterSplits(record.stream)]) {
        const live = straight(utf8(record.stream.slice(0, split)), "utf-8", record.cols, record.rows);
        const viewer = emulator("utf-8", record.cols, record.rows);
        viewer.process(paint(live.ansiScreen(), "utf-8"));
        viewer.process(utf8(record.stream.slice(split)));
        expect(state(viewer), `split at character ${split}`).toStrictEqual(whole);
      }
    });
  }

  it("draws an empty cell as a blank when the cursor is deferred over it", () => {
    // A resize can leave the cursor deferred past the last column over two
    // empty second halves; the reference draws an empty cell as a blank.
    const live = straight(utf8("\u4e2d\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H\x1b[P\x1b[1;3H"), "utf-8", 4, 1);
    live.resize(2, 1);
    expect(suffix(live)).toContain("\x1b[1;1H\x1b[0m \x1b[20l");
  });

  it("draws an empty cell as a blank on a one-column screen", () => {
    // An empty second half with no cell left of it and no neighbouring row.
    const live = straight(utf8("\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H"), "utf-8", 2, 1);
    live.resize(1, 1);
    expect(suffix(live)).toContain("\x1b[1;1H\x1b[0m \x1b[20l");
  });

  it("draws a wide character once, with nothing for its second cell", () => {
    const screen = new Screen(6, 1);
    new Stream(screen).feed("\u4e2da");
    expect(renderCellRows((y, x) => screen.peek(y, x), 6, 1)).toStrictEqual(["\x1b[0m\u4e2da   \x1b[0m"]);
  });

  it("leaves blank a lost first half with no cell left of it to recreate it from", () => {
    // Deleting a wide character's first half leaves its second cell empty. A
    // second such cell after it in the last column has no cell left of it to
    // recreate it from: the first is recreated and the second left blank.
    const cells: Array<Char | undefined> = [
      { ...defaultChar(), data: "a" },
      { ...defaultChar(), data: "b" },
      { ...defaultChar(), data: "" },
      { ...defaultChar(), data: "" },
    ];
    expect(renderCellRows((_y, x) => cells[x], 4, 1)).toStrictEqual(["\x1b[0mab\u3000\x1b[3G\x1b[P\x1b[C\x1b[0m"]);
    expect(renderCellRows((_y, x) => (x === 0 ? cells[2] : undefined), 1, 1)).toStrictEqual(["\x1b[0m"]);
  });
});

describe("modes", () => {
  it("restores insert mode", () => {
    const stream = bytes("abcdef\r\x1b[4hXY");
    expect(suffix(straight(stream.subarray(0, stream.length - 2), "utf-8", 20, 3))).toContain("\x1b[4h");
    expectResumes(stream, stream.length - 2, "utf-8", 20, 3);
  });

  it("restores origin mode, with the cursor's row counted from the region", () => {
    const stream = bytes("\x1b[2;3r\x1b[?6h\x1b[2;2Hab");
    expect(suffix(straight(stream, "utf-8", 20, 3))).toContain(
      "\x1b[2;3r\x1b[?6h\x1b[?7h\x1b[4l\x1b[2;4H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h",
    );
    expectResumes(bytes("\x1b[2;3r\x1b[?6h\x1b[2;2Hab\x1b[1;1Hx\nyz"), stream.length, "utf-8", 20, 3);
  });

  it("restores autowrap off after the deferred-wrap redraw", () => {
    const stream = bytes(`\x1b[?7l${"x".repeat(20)}`);
    expect(suffix(straight(stream, "utf-8", 20, 3))).toContain(
      "\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;20H\x1b[0mx\x1b[?7l\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h",
    );
    expectResumes(bytes(`\x1b[?7l${"x".repeat(20)}yz`), stream.length, "utf-8", 20, 3);
  });

  it("restores new line mode", () => {
    const stream = bytes("\x1b[20h");
    expect(suffix(straight(stream, "utf-8", 20, 3))).toContain(
      "\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;1H\x1b[20h\x1b[?1l\x1b[0m\x1b[?25h",
    );
    expectResumes(bytes("\x1b[20hab\ncd"), stream.length, "utf-8", 20, 3);
  });

  it("sends the reset values of the modes this screen does not model", () => {
    expect(suffix(straight(bytes(""), "utf-8", 20, 3))).toBe(
      "\x1b[0m                    \x1b[0m\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;1H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h",
    );
  });
});

describe("character sets", () => {
  it("restores the designations and the shift in a UTF-8 session", () => {
    const stream = bytes("\x1b)0\x0eqqq\x1b(0x");
    expect(suffix(straight(stream, "utf-8", 20, 3))).toContain("\x1b(0\x1b)0\x0e");
    expectResumes(bytes("\x1b)0\x0eqqq\x1b(0xqq\x0fqq\x1b(Bqq"), stream.length, "utf-8", 20, 3);
    expect(suffix(straight(bytes("\x1b)B"), "utf-8", 20, 3))).toContain("\x1b(B\x1b)B");
  });

  it("restores codepage 437 and VAX42 designations", () => {
    const stream = bytes("\x1b(U\x1b)V\x0e\xc3\x8d");
    expect(suffix(straight(stream, "utf-8", 20, 3))).toContain("\x1b(U\x1b)V\x0e");
    expectResumes(bytes("\x1b(U\x1b)V\x0e\xc3\x8d\x0f\xc3\x8d"), stream.length, "utf-8", 20, 3);
  });

  it("sends nothing for the power-on sets nothing has designated", () => {
    const tail = suffix(straight(bytes(""), "utf-8", 20, 3));
    expect(tail).not.toContain("\x1b(");
    expect(tail).not.toContain("\x1b)");
  });

  it("sends a designation that matches the power-on sets", () => {
    // curses under TERM=screen designates G1 as graphics once, then draws
    // every line with SO/SI; a viewer's G1 is ASCII until told.
    const stream = bytes("\x1b(B\x1b)0\x0eqq\x0f");
    const live = straight(stream, "utf-8", 20, 3);
    expect(suffix(live)).toContain("\x1b(B\x1b)0\x1b[0m");
    expectResumes(bytes("\x1b(B\x1b)0\x0eqq\x0f\x0eqqqq\x0f"), stream.length, "utf-8", 20, 3);
    live.reset();
    expect(suffix(live)).not.toContain("\x1b)");
  });

  it("sends nothing in a CP437 session, which ignores them", () => {
    const tail = suffix(straight(bytes("\x1b)0\x0e"), "cp437", 20, 3));
    expect(tail).not.toContain("\x1b(");
    expect(tail).not.toContain("\x0e");
  });
});
