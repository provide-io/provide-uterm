//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import {
  ANSI_ALT_SCREEN,
  ANSI_EXIT_ALT,
  ANSI_HIDE_CURSOR,
  ANSI_SHOW_CURSOR,
  type CellStyle,
  clearScreen,
  moveTo,
  renderCellRows,
  styleToSgr,
} from "../render/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { TerminalEmulator } from "./index.ts";

interface EmulatorGolden {
  defaults: { cols: number; rows: number; term: string; receive_encoding: string };
  cases: Array<{
    name: string;
    cols: number;
    rows: number;
    chunks: string[];
    snapshot: Record<string, unknown>;
    ansi_screen: string;
    raw_tail: string;
  }>;
  after_resize: { snapshot: Record<string, unknown>; raw_tail: string };
  resize_order_divergence: { read_first: string; never_read: string };
  after_reset: { snapshot: Record<string, unknown>; raw_tail: string };
  styles: Array<{ style: [string, string, boolean, boolean, boolean, boolean]; sgr: string }>;
  empty_rows: string[];
}

const golden = loadGolden<EmulatorGolden>("emulator_golden.json");

/** Encode as CP437 the way a transport delivers bytes. */
function cp437(text: string): Uint8Array {
  return new Uint8Array(Buffer.from(text, "latin1"));
}

/** Build an emulator and feed it the chunks of a corpus case. */
function drive(cols: number, rows: number, chunks: readonly string[]): TerminalEmulator {
  const emulator = new TerminalEmulator({ cols, rows });
  for (const chunk of chunks) {
    emulator.process(cp437(chunk));
  }
  return emulator;
}

/** A snapshot with the always-fresh timestamp removed. */
function stableSnapshot(emulator: TerminalEmulator): Record<string, unknown> {
  const { captured_at: _ignored, ...rest } = emulator.getSnapshot();
  return rest;
}

/** Turn the corpus tuple form into the style object the port takes. */
function toStyle(tuple: [string, string, boolean, boolean, boolean, boolean]): CellStyle {
  return {
    fg: tuple[0],
    bg: tuple[1],
    bold: tuple[2],
    underscore: tuple[3],
    reverse: tuple[4],
    blink: tuple[5],
  };
}

describe("TerminalEmulator defaults", () => {
  it("matches the documented default geometry and terminal type", () => {
    const emulator = new TerminalEmulator();
    expect({
      cols: emulator.cols,
      rows: emulator.rows,
      term: emulator.term,
      receive_encoding: emulator.receiveEncoding,
    }).toStrictEqual(golden.defaults);
  });
});

describe("TerminalEmulator.process", () => {
  it("decodes incoming bytes as CP437 by default", () => {
    const emulator = new TerminalEmulator({ cols: 8, rows: 1 });
    emulator.process(new Uint8Array([0xb0, 0xb1]));
    expect(emulator.getSnapshot().screen.slice(0, 2)).toBe("░▒");
  });

  it("honours a UTF-8 receive encoding", () => {
    const emulator = new TerminalEmulator({ cols: 8, rows: 1, receiveEncoding: "utf-8" });
    emulator.process(new Uint8Array(Buffer.from("你", "utf-8")));
    expect(emulator.getSnapshot().screen.slice(0, 1)).toBe("你");
  });

  it("accumulates a raw tail with escapes intact", () => {
    const emulator = drive(20, 2, ["\x1b[31mred"]);
    expect(emulator.getRawTail()).toBe("\x1b[31mred");
  });

  it("bounds the raw tail and keeps the newest output", () => {
    const emulator = new TerminalEmulator({ cols: 20, rows: 2 });
    emulator.process(cp437("a".repeat(5000)));
    emulator.process(cp437("END"));
    const tail = emulator.getRawTail();
    expect(tail).toHaveLength(4096);
    expect(tail.endsWith("END")).toBe(true);
  });

  it("leaves the tail alone for an empty write", () => {
    const emulator = drive(20, 2, ["abc"]);
    emulator.process(new Uint8Array());
    expect(emulator.getRawTail()).toBe("abc");
  });
});

describe("TerminalEmulator.getSnapshot", () => {
  it("stamps a fresh timestamp on every call", () => {
    const emulator = drive(20, 2, ["abc"]);
    const first = emulator.getSnapshot().captured_at;
    const second = emulator.getSnapshot().captured_at;
    expect(second).toBeGreaterThanOrEqual(first);
  });

  it("reuses the cached body until more output arrives", () => {
    const emulator = drive(20, 2, ["abc"]);
    const first = emulator.getSnapshot();
    const second = emulator.getSnapshot();
    expect(second.screen_hash).toBe(first.screen_hash);
    emulator.process(cp437("d"));
    expect(emulator.getSnapshot().screen_hash).not.toBe(first.screen_hash);
  });

  it("returns a copy of the cursor rather than the live one", () => {
    const emulator = drive(20, 2, ["abc"]);
    const snapshot = emulator.getSnapshot();
    snapshot.cursor.x = 99;
    expect(emulator.getSnapshot().cursor.x).toBe(3);
  });

  it("hashes the screen text with SHA-256", () => {
    expect(drive(20, 2, ["abc"]).getSnapshot().screen_hash).toMatch(/^[0-9a-f]{64}$/);
  });

  it("detects a trailing colon, not a trailing space, on a one-row screen", () => {
    // Despite the field name, the comparison only differs when a colon is
    // present: trailing spaces fall to both strips alike. "Command? " is
    // false and "Name:" is true, which is the reference's behaviour.
    expect(drive(20, 1, ["Name:"]).getSnapshot().has_trailing_space).toBe(true);
    expect(drive(20, 1, ["Name: "]).getSnapshot().has_trailing_space).toBe(true);
    expect(drive(20, 1, ["Command? "]).getSnapshot().has_trailing_space).toBe(false);
    expect(drive(20, 1, ["done"]).getSnapshot().has_trailing_space).toBe(false);
  });

  it("always reports true on a multi-row screen, matching the reference", () => {
    // The two strips are asymmetric: the second removes only spaces and
    // colons, so it stops at the newline separating rows. Any screen with a
    // blank row below the content therefore differs under the two strips
    // whatever the content is, and the flag reports true unconditionally.
    // Reproduced rather than corrected, because a consumer keying off it
    // would otherwise see different values from Python and this port — but
    // it does mean the flag carries no information above one row.
    expect(drive(20, 2, ["done"]).getSnapshot().has_trailing_space).toBe(true);
    expect(drive(20, 2, ["Command? "]).getSnapshot().has_trailing_space).toBe(true);
    expect(drive(20, 5, [""]).getSnapshot().has_trailing_space).toBe(true);
  });

  it("treats a blank screen as having the cursor at the end", () => {
    expect(drive(20, 2, []).getSnapshot().cursor_at_end).toBe(true);
  });

  it("allows two characters of slack for a BBS caret", () => {
    // "abcdef" with the cursor at column 4 is still "at the prompt".
    expect(drive(20, 2, ["abcdef\x1b[5G"]).getSnapshot().cursor_at_end).toBe(true);
    expect(drive(20, 2, ["abcdef\x1b[1G"]).getSnapshot().cursor_at_end).toBe(false);
  });
});

describe("TerminalEmulator.reset and resize", () => {
  it("clears the screen on reset but keeps the raw tail", () => {
    const emulator = drive(20, 3, ["hello"]);
    emulator.reset();
    expect(emulator.getSnapshot().screen.trim()).toBe("");
    expect(emulator.getRawTail()).toBe("hello");
  });

  it("reports the new geometry after a resize", () => {
    const emulator = drive(20, 5, ["hello"]);
    emulator.resize(10, 3);
    const snapshot = emulator.getSnapshot();
    expect({ cols: snapshot.cols, rows: snapshot.rows }).toStrictEqual({ cols: 10, rows: 3 });
    expect(snapshot.screen.split("\n")).toHaveLength(3);
  });

  it("keeps the newest rows when shrinking", () => {
    const emulator = drive(10, 5, ["a\r\nb\r\nc\r\nd\r\ne"]);
    emulator.resize(10, 3);
    expect(
      emulator
        .getSnapshot()
        .screen.split("\n")
        .map((line) => line.trim()),
    ).toStrictEqual(["c", "d", "e"]);
  });

  it("clips columns when narrowing", () => {
    const emulator = drive(10, 1, ["abcdefghij"]);
    emulator.resize(4, 1);
    expect(emulator.getSnapshot().screen).toBe("abcd");
  });
});

describe("styleToSgr", () => {
  it("emits a plain reset for a style with nothing to say", () => {
    expect(
      styleToSgr({ fg: "default", bg: "default", bold: false, underscore: false, reverse: false, blink: false }),
    ).toBe("\x1b[0m");
  });

  it("swaps the colours for reverse video rather than emitting SGR 7", () => {
    const style: CellStyle = { fg: "red", bg: "green", bold: false, underscore: false, reverse: true, blink: false };
    expect(styleToSgr(style)).toBe("\x1b[32;41m");
  });

  it("emits truecolor for a hex colour", () => {
    const style: CellStyle = {
      fg: "ff8000",
      bg: "default",
      bold: false,
      underscore: false,
      reverse: false,
      blink: false,
    };
    expect(styleToSgr(style)).toBe("\x1b[38;2;255;128;0m");
  });

  it("emits no colour for a name the table does not carry", () => {
    // The screen model names its yellow "brown", which this table lacks, so
    // a brown cell loses its colour. That is the reference's behaviour.
    const style: CellStyle = {
      fg: "brown",
      bg: "default",
      bold: false,
      underscore: false,
      reverse: false,
      blink: false,
    };
    expect(styleToSgr(style)).toBe("\x1b[0m");
  });
});

describe("cursor and screen control sequences", () => {
  it("builds a one-based cursor position sequence", () => {
    expect(moveTo(3, 5)).toBe("\x1b[3;5H");
  });

  it("builds the erase-screen sequence", () => {
    expect(clearScreen()).toBe("\x1b[2J");
  });

  it("exposes the cursor and alternate-screen sequences", () => {
    expect({ ANSI_HIDE_CURSOR, ANSI_SHOW_CURSOR, ANSI_ALT_SCREEN, ANSI_EXIT_ALT }).toStrictEqual({
      ANSI_HIDE_CURSOR: "\x1b[?25l",
      ANSI_SHOW_CURSOR: "\x1b[?25h",
      ANSI_ALT_SCREEN: "\x1b[?1049h",
      ANSI_EXIT_ALT: "\x1b[?1049l",
    });
  });
});

describe("renderCellRows", () => {
  it("emits a reset per row for an empty buffer", () => {
    expect(renderCellRows(() => undefined, 3, 2)).toStrictEqual(golden.empty_rows);
  });

  it("treats an empty colour or character as the default", () => {
    // The reference coalesces with `or "default"`, so a cell carrying an
    // empty string renders as unstyled rather than as a stray escape. The
    // screen model never produces one, but the renderer takes any reader.
    const blankish = renderCellRows(
      () => ({
        data: "",
        fg: "",
        bg: "",
        bold: false,
        italics: false,
        underscore: false,
        reverse: false,
        strikethrough: false,
        blink: false,
      }),
      2,
      1,
    );
    expect(blankish).toStrictEqual(["\x1b[0m  \x1b[0m"]);
  });

  it("emits an escape only where the style changes", () => {
    const emulator = drive(6, 1, ["\x1b[31maa\x1b[0mbb"]);
    const row = emulator.ansiScreen();
    // Two style runs — red, then default for the rest of the row — plus the
    // trailing reset. Not one escape per cell.
    expect(row.match(/\x1b\[/g)?.length).toBe(3);
  });
});

describe("differential parity with CPython", () => {
  it("matches every recorded snapshot, ANSI screen and raw tail", () => {
    for (const testCase of golden.cases) {
      const emulator = drive(testCase.cols, testCase.rows, testCase.chunks);
      expect({
        name: testCase.name,
        snapshot: stableSnapshot(emulator),
        ansi: emulator.ansiScreen(),
        tail: emulator.getRawTail(),
      }).toStrictEqual({
        name: testCase.name,
        snapshot: testCase.snapshot,
        ansi: testCase.ansi_screen,
        tail: testCase.raw_tail,
      });
    }
    expect(golden.cases.length).toBeGreaterThan(15);
  });

  it("resizes the same way whether or not the screen was read first", () => {
    // The reference is order-dependent here: reading the screen materialises
    // the blank rows of its defaultdict-backed buffer, and a later shrink
    // then shifts those rows over the content. So "hello" survives a resize
    // on a never-read emulator and is lost on a read one. This port is
    // deterministic and always clips at the top, which is what the reference
    // documents and what a server that snapshots continuously already sees.
    const read = new TerminalEmulator({ cols: 20, rows: 5 });
    read.process(cp437("hello"));
    read.getSnapshot();
    read.resize(10, 3);

    const unread = new TerminalEmulator({ cols: 20, rows: 5 });
    unread.process(cp437("hello"));
    unread.resize(10, 3);

    expect(read.getSnapshot().screen).toBe(unread.getSnapshot().screen);
    expect(read.getSnapshot().screen).toBe(golden.resize_order_divergence.read_first);
    expect(golden.resize_order_divergence.never_read).not.toBe(golden.resize_order_divergence.read_first);
  });

  it("matches the recorded state after a resize", () => {
    const emulator = new TerminalEmulator({ cols: 20, rows: 5 });
    emulator.process(cp437("hello"));
    emulator.getSnapshot();
    emulator.resize(10, 3);
    expect({ snapshot: stableSnapshot(emulator), tail: emulator.getRawTail() }).toStrictEqual({
      snapshot: golden.after_resize.snapshot,
      tail: golden.after_resize.raw_tail,
    });
  });

  it("matches the recorded state after a reset", () => {
    const emulator = new TerminalEmulator({ cols: 20, rows: 5 });
    emulator.process(cp437("hello"));
    emulator.reset();
    expect({ snapshot: stableSnapshot(emulator), tail: emulator.getRawTail() }).toStrictEqual({
      snapshot: golden.after_reset.snapshot,
      tail: golden.after_reset.raw_tail,
    });
  });

  it("matches every recorded style rendering", () => {
    for (const record of golden.styles) {
      expect({ style: record.style, sgr: styleToSgr(toStyle(record.style)) }).toStrictEqual(record);
    }
    expect(golden.styles.length).toBeGreaterThan(15);
  });
});
