//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  CP437_TABLE,
  cleanScreenForDisplay,
  decodeCp437,
  encodeCp437,
  extractActionTags,
  extractKeyValuePairs,
  extractMenuOptions,
  extractNumberedList,
  normalizeTerminalText,
  stripAnsi,
} from "./index.ts";

interface ScreenGolden {
  cp437_table: number[];
  cp437_encode: Array<{ text: string; bytes: string }>;
  normalize: Array<{ text: string; out: string }>;
  action_tags: Array<{ text: string; max_tags: number; out: string[] }>;
  clean_screen: Array<{ screen: string; max_lines: number; out: string[] }>;
  menu_options: Array<{ screen: string; pattern: string | null; out: string[][] }>;
  numbered_list: Array<{ screen: string; pattern: string | null; out: string[][] }>;
  key_values: Array<{ screen: string; patterns: Record<string, string>; out: Record<string, string> }>;
}

const golden = loadGolden<ScreenGolden>("screen_golden.json");

/** Render bytes as lowercase hex, matching Python's `bytes.hex()`. */
function hex(data: Uint8Array): string {
  return Buffer.from(data).toString("hex");
}

describe("CP437", () => {
  it("matches the reference table byte for byte", () => {
    expect([...CP437_TABLE]).toStrictEqual(golden.cp437_table);
  });

  it("passes ASCII through unchanged", () => {
    expect(decodeCp437(new Uint8Array([0x41, 0x42]))).toBe("AB");
  });

  it("decodes the high range to box-drawing and accented characters", () => {
    expect(decodeCp437(new Uint8Array([0xb0, 0xb1, 0xb2]))).toBe("░▒▓");
    expect(decodeCp437(new Uint8Array([0xc4, 0xb3, 0xda]))).toBe("─│┌");
  });

  it("round-trips every byte value", () => {
    const all = new Uint8Array(256);
    for (let i = 0; i < 256; i += 1) {
      all[i] = i;
    }
    expect(hex(encodeCp437(decodeCp437(all)))).toBe(hex(all));
  });

  it("replaces a character with no CP437 representation", () => {
    expect(hex(encodeCp437("你"))).toBe("3f");
    expect(hex(encodeCp437("a你b"))).toBe("613f62");
  });
});

describe("normalizeTerminalText", () => {
  it("returns an empty string for empty input", () => {
    expect(normalizeTerminalText("")).toBe("");
  });

  it("normalises both line-ending forms", () => {
    expect(normalizeTerminalText("a\r\nb")).toBe("a\nb");
    expect(normalizeTerminalText("a\rb")).toBe("a\nb");
  });

  it("removes CSI sequences", () => {
    expect(normalizeTerminalText("\x1b[31mred\x1b[0m")).toBe("red");
    expect(normalizeTerminalText("\x1b[?25l")).toBe("");
  });

  it("removes two-character escapes", () => {
    expect(normalizeTerminalText("\x1bMx")).toBe("x");
  });

  it("leaves an escape with no final byte alone", () => {
    expect(normalizeTerminalText("\x1b")).toBe("\x1b");
  });

  it("removes an isolated bare SGR fragment at the start of a line", () => {
    expect(normalizeTerminalText("1;31mHELLO")).toBe("HELLO");
    expect(normalizeTerminalText("a\n1;31mB")).toBe("a\nB");
  });

  it("keeps a bare fragment that is not isolated", () => {
    expect(normalizeTerminalText("abc1;31mdef")).toBe("abc1;31mdef");
  });

  it("is exposed under the stripAnsi alias", () => {
    expect(stripAnsi("\x1b[31mred")).toBe(normalizeTerminalText("\x1b[31mred"));
  });
});

describe("extractActionTags", () => {
  it("returns nothing for empty input", () => {
    expect(extractActionTags("")).toStrictEqual([]);
  });

  it("extracts a tag and trims its whitespace", () => {
    expect(extractActionTags("<  Move  >")).toStrictEqual(["Move"]);
  });

  it("de-duplicates case-insensitively, keeping the first spelling", () => {
    expect(extractActionTags("<Move> <move> <MOVE>")).toStrictEqual(["Move"]);
  });

  it("skips an empty or whitespace-only tag", () => {
    expect(extractActionTags("<> <Move>")).toStrictEqual(["Move"]);
    expect(extractActionTags("<   > <Move>")).toStrictEqual(["Move"]);
  });

  it("honours the cap, raising anything below one to one", () => {
    expect(extractActionTags("<a> <b> <c>", 2)).toStrictEqual(["a", "b"]);
    expect(extractActionTags("<a> <b>", 0)).toStrictEqual(["a"]);
  });

  it("does not match a tag spanning a line break", () => {
    expect(extractActionTags("<a\nb>")).toStrictEqual([]);
  });

  it("does not match a tag longer than eighty characters", () => {
    expect(extractActionTags(`<${"x".repeat(81)}>`)).toStrictEqual([]);
  });
});

describe("cleanScreenForDisplay", () => {
  it("keeps non-empty lines", () => {
    expect(cleanScreenForDisplay("one\ntwo")).toStrictEqual(["one", "two"]);
  });

  it("drops a line of exactly eighty spaces as padding", () => {
    expect(cleanScreenForDisplay(" ".repeat(80))).toStrictEqual([]);
  });

  it("keeps a shorter run of spaces", () => {
    expect(cleanScreenForDisplay(" ".repeat(79))).toStrictEqual([" ".repeat(79)]);
  });

  it("stops at the line cap", () => {
    expect(cleanScreenForDisplay("a\nb\nc", 2)).toStrictEqual(["a", "b"]);
  });
});

describe("extractMenuOptions", () => {
  it("extracts the three common bracket styles", () => {
    expect(extractMenuOptions("<A> Move")).toStrictEqual([["A", "Move"]]);
    expect(extractMenuOptions("[B] Attack")).toStrictEqual([["B", "Attack"]]);
    expect(extractMenuOptions("(C) Flee")).toStrictEqual([["C", "Flee"]]);
  });

  it("does not match a lowercase key with the default pattern", () => {
    expect(extractMenuOptions("<a> move")).toStrictEqual([]);
  });

  it("accepts a custom pattern with two groups", () => {
    expect(extractMenuOptions("A=Move", "([A-Z])=(\\w+)")).toStrictEqual([["A", "Move"]]);
  });

  it("returns nothing for an invalid pattern rather than raising", () => {
    expect(extractMenuOptions("<A> Move", "([A-Z]")).toStrictEqual([]);
  });
});

describe("extractNumberedList", () => {
  it("extracts dot and paren separators", () => {
    expect(extractNumberedList("1. First")).toStrictEqual([["1", "First"]]);
    expect(extractNumberedList("2) Second")).toStrictEqual([["2", "Second"]]);
  });

  it("does not match a dash separator with the default pattern", () => {
    expect(extractNumberedList("1 - Dashed")).toStrictEqual([]);
  });

  it("returns nothing for an invalid pattern rather than raising", () => {
    expect(extractNumberedList("1. First", "(\\d+")).toStrictEqual([]);
  });
});

describe("extractKeyValuePairs", () => {
  it("extracts each configured field", () => {
    expect(extractKeyValuePairs("Credits: 1,234", { credits: "Credits?:?\\s*([\\d,]+)" })).toStrictEqual({
      credits: "1,234",
    });
  });

  it("matches case-insensitively", () => {
    expect(extractKeyValuePairs("credits: 99", { credits: "Credits:\\s*(\\d+)" })).toStrictEqual({ credits: "99" });
  });

  it("omits a field that does not match", () => {
    expect(extractKeyValuePairs("nothing", { credits: "Credits:\\s*(\\d+)" })).toStrictEqual({});
  });

  it("skips an invalid pattern rather than raising", () => {
    expect(extractKeyValuePairs("Credits: 5", { bad: "([0-9]", credits: "Credits:\\s*(\\d+)" })).toStrictEqual({
      credits: "5",
    });
  });
});

describe("differential parity with CPython", () => {
  it("matches every CP437 encode record", () => {
    for (const record of golden.cp437_encode) {
      expect({ text: record.text, bytes: hex(encodeCp437(record.text)) }).toStrictEqual(record);
    }
  });

  it("matches every normalise record", () => {
    for (const record of golden.normalize) {
      expect({ text: record.text, out: normalizeTerminalText(record.text) }).toStrictEqual(record);
    }
    expect(golden.normalize.length).toBeGreaterThan(25);
  });

  it("matches every action-tag record", () => {
    for (const record of golden.action_tags) {
      expect(extractActionTags(record.text, record.max_tags)).toStrictEqual(record.out);
    }
    expect(golden.action_tags.length).toBeGreaterThan(12);
  });

  it("matches every clean-screen record", () => {
    for (const record of golden.clean_screen) {
      expect(cleanScreenForDisplay(record.screen, record.max_lines)).toStrictEqual(record.out);
    }
  });

  it("matches every menu-option record", () => {
    for (const record of golden.menu_options) {
      expect(extractMenuOptions(record.screen, record.pattern ?? undefined)).toStrictEqual(
        record.out.map((pair) => [pair[0], pair[1]]),
      );
    }
  });

  it("matches every numbered-list record", () => {
    for (const record of golden.numbered_list) {
      expect(extractNumberedList(record.screen, record.pattern ?? undefined)).toStrictEqual(
        record.out.map((pair) => [pair[0], pair[1]]),
      );
    }
  });

  it("matches every key-value record", () => {
    for (const record of golden.key_values) {
      expect(extractKeyValuePairs(record.screen, record.patterns)).toStrictEqual(record.out);
    }
  });
});
