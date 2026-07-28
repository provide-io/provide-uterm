//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  BANNER,
  CLEAR_SCREEN,
  DEFAULT_KEY_WIDTH,
  errorMsg,
  fmtKv,
  fmtTable,
  heading,
  infoMsg,
  PROMPT,
  successMsg,
} from "./index.ts";

interface ShellGolden {
  prompt: string;
  banner: string;
  clear_screen: string;
  messages: Array<{ name: string; text: string; error: string; info: string; success: string; heading: string }>;
  kv: Array<{ name: string; key: string; value: string; width: number; line: string }>;
  tables: Array<{ name: string; rows: string[][]; headers: string[] | null; table: string }>;
}

const golden = loadGolden<ShellGolden>("shelloutput_golden.json");

describe("the shell's fixed strings", () => {
  it("shows the prompt, banner and clear the reference does", () => {
    expect(PROMPT).toBe(golden.prompt);
    expect(BANNER).toBe(golden.banner);
    expect(CLEAR_SCREEN).toBe(golden.clear_screen);
  });

  it("erases before homing the cursor", () => {
    // The other order leaves the cursor wherever the erase put it.
    expect(CLEAR_SCREEN).toBe("\x1b[2J\x1b[H");
  });
});

describe("the shell's messages", () => {
  it.each(golden.messages)("$name", (record) => {
    expect(errorMsg(record.text)).toBe(record.error);
    expect(infoMsg(record.text)).toBe(record.info);
    expect(successMsg(record.text)).toBe(record.success);
    expect(heading(record.text)).toBe(record.heading);
  });

  it("ends every line with a carriage return as well", () => {
    // A terminal in raw mode does not translate one into the other, so a bare
    // newline drops a line without returning the cursor.
    for (const line of [errorMsg("x"), infoMsg("x"), successMsg("x"), heading("x"), fmtKv("k", "v")]) {
      expect(line.endsWith("\r\n")).toBe(true);
    }
  });

  it("closes every colour it opens", () => {
    // An unclosed colour leaks into whatever the shell prints next.
    for (const line of [errorMsg("x"), infoMsg("x"), successMsg("x"), heading("x")]) {
      expect(line).toContain("\x1b[0m");
    }
  });
});

describe("a labelled value", () => {
  it.each(golden.kv)("$name", (record) => {
    expect(fmtKv(record.key, record.value, record.width)).toBe(record.line);
  });

  it("pads a key to the column", () => {
    expect(fmtKv("name", "value")).toContain(`name${" ".repeat(DEFAULT_KEY_WIDTH - 4)}`);
  });

  it("does not clip a key wider than the column", () => {
    // The value moves right instead: a key clipped in half tells a reader
    // less than a ragged column does.
    // The reset sits between the key and the value, so they are checked as
    // the line actually renders rather than as a guess at it.
    const wide = "123456789012345678901234";
    expect(fmtKv(wide, "value", 20)).toBe(
      golden.kv.find((entry) => entry.name === "a key longer than the width")?.line,
    );
    expect(fmtKv(wide, "value", 20)).toContain(wide);
  });
});

describe("a table", () => {
  it.each(golden.tables)("$name", (record) => {
    expect(fmtTable(record.rows, record.headers ?? undefined)).toBe(record.table);
  });

  it("says so when there is nothing to show", () => {
    // A caller printing nothing at all leaves a user unsure whether the
    // command ran.
    expect(fmtTable([])).toBe(infoMsg("(no results)"));
    expect(fmtTable([], ["a", "b"])).toBe(infoMsg("(no results)"));
  });

  it("makes a column as wide as its widest cell", () => {
    expect(fmtTable([["a"], ["bbb"]])).toBe("  a  \r\n  bbb\r\n");
  });

  it("widens a column to fit its header", () => {
    // Computed from the rows first, then raised — so a header longer than any
    // value still fits.
    expect(fmtTable([["a"]], ["header"])).toContain("------");
  });

  it("does not narrow a column to its header", () => {
    // And one shorter than a value does not clip it.
    expect(fmtTable([["aaaaaaaa"]], ["h"])).toContain("aaaaaaaa");
  });

  it("truncates the table to its shortest row", () => {
    // The reference zips the rows together and stops at the shortest. A port
    // that padded instead would render a table the reference never would, so
    // this is pinned rather than corrected.
    expect(fmtTable([["a", "b"], ["c"]])).toBe("  a\r\n  c\r\n");
    expect(fmtTable([["c"], ["a", "b"]])).toBe("  c\r\n  a\r\n");
  });

  it("truncates to a short header list too", () => {
    expect(fmtTable([["a", "b"]], ["one"])).toBe(
      golden.tables.find((e) => e.name === "fewer headers than columns")?.table,
    );
  });

  it("ignores headers past the last column", () => {
    expect(fmtTable([["a"]], ["one", "two"])).toBe(
      golden.tables.find((e) => e.name === "more headers than columns")?.table,
    );
  });

  it("rules under the headers to the same widths", () => {
    const table = fmtTable(
      [
        ["a", "b"],
        ["cc", "dd"],
      ],
      ["one", "two"],
    );
    const [, rule] = table.split("\r\n");
    expect(rule).toBe("  ---  ---");
  });

  it("carries an empty cell as blank rather than dropping it", () => {
    expect(fmtTable([["", "b"]])).toBe("    b\r\n");
  });
});
