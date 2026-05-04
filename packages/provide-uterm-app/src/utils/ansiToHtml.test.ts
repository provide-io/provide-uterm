//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import { ansiToSegments, stripAnsi } from "./ansiToHtml";

describe("ansiToSegments", () => {
  it("returns plain text as a single unstyled segment", () => {
    const result = ansiToSegments("hello world");
    expect(result).toEqual([{ text: "hello world", style: "" }]);
  });

  it("returns empty array for empty input", () => {
    expect(ansiToSegments("")).toEqual([]);
  });

  it("handles reset (SGR 0)", () => {
    const result = ansiToSegments("\x1b[1mBold\x1b[0mNormal");
    expect(result).toHaveLength(2);
    expect(result[0]?.style).toContain("font-weight:bold");
    expect(result[1]?.style).toBe("");
    expect(result[1]?.text).toBe("Normal");
  });

  describe("standard colors", () => {
    it("applies foreground color 30-37", () => {
      const result = ansiToSegments("\x1b[31mred text\x1b[0m");
      expect(result[0]?.style).toContain("color:#cc0000");
    });

    it("applies background color 40-47", () => {
      const result = ansiToSegments("\x1b[42mgreen bg\x1b[0m");
      expect(result[0]?.style).toContain("background:#00cc00");
    });

    it("applies bright foreground 90-97", () => {
      const result = ansiToSegments("\x1b[91mbright red\x1b[0m");
      expect(result[0]?.style).toContain("color:#ef2929");
    });

    it("applies bright background 100-107", () => {
      const result = ansiToSegments("\x1b[102mbright green bg\x1b[0m");
      expect(result[0]?.style).toContain("background:#8ae234");
    });
  });

  describe("text attributes", () => {
    it("applies bold", () => {
      const result = ansiToSegments("\x1b[1mbold\x1b[0m");
      expect(result[0]?.style).toContain("font-weight:bold");
    });

    it("applies dim", () => {
      const result = ansiToSegments("\x1b[2mdim\x1b[0m");
      expect(result[0]?.style).toContain("opacity:0.6");
    });

    it("applies italic", () => {
      const result = ansiToSegments("\x1b[3mitalic\x1b[0m");
      expect(result[0]?.style).toContain("font-style:italic");
    });

    it("applies underline", () => {
      const result = ansiToSegments("\x1b[4munderline\x1b[0m");
      expect(result[0]?.style).toContain("text-decoration:underline");
    });

    it("resets bold/dim with SGR 22", () => {
      const result = ansiToSegments("\x1b[1m\x1b[2mBD\x1b[22mNormal");
      expect(result[1]?.style).not.toContain("font-weight:bold");
      expect(result[1]?.style).not.toContain("opacity:0.6");
    });

    it("resets italic with SGR 23", () => {
      const result = ansiToSegments("\x1b[3mI\x1b[23mN");
      expect(result[1]?.style).not.toContain("font-style:italic");
    });

    it("resets underline with SGR 24", () => {
      const result = ansiToSegments("\x1b[4mU\x1b[24mN");
      expect(result[1]?.style).not.toContain("text-decoration:underline");
    });
  });

  describe("inverse", () => {
    it("swaps foreground and background with SGR 7", () => {
      const result = ansiToSegments("\x1b[31m\x1b[42m\x1b[7minverted\x1b[0m");
      // fg was red, bg was green; after inverse: fg becomes green bg, bg becomes red fg
      expect(result[0]?.style).toContain("color:#00cc00");
      expect(result[0]?.style).toContain("background:#cc0000");
    });

    it("resets inverse with SGR 27", () => {
      const result = ansiToSegments("\x1b[31m\x1b[7mI\x1b[27mN");
      // After un-inverse, fg is back to red
      expect(result[1]?.style).toContain("color:#cc0000");
      expect(result[1]?.style).not.toContain("background");
    });
  });

  describe("default color resets", () => {
    it("SGR 39 resets foreground", () => {
      const result = ansiToSegments("\x1b[31mR\x1b[39mD");
      expect(result[1]?.style).not.toContain("color:");
    });

    it("SGR 49 resets background", () => {
      const result = ansiToSegments("\x1b[41mR\x1b[49mD");
      expect(result[1]?.style).not.toContain("background:");
    });
  });

  describe("256-color mode", () => {
    it("handles standard 16 colors via 38;5;n", () => {
      const result = ansiToSegments("\x1b[38;5;1mred\x1b[0m");
      expect(result[0]?.style).toContain("color:#cc0000");
    });

    it("handles 6x6x6 cube colors (16-231)", () => {
      const result = ansiToSegments("\x1b[38;5;196mtest\x1b[0m");
      // Color 196 = idx 180, r=Math.floor(180/36)*51 = 255, g=0, b=0
      expect(result[0]?.style).toContain("color:rgb(255,0,0)");
    });

    it("handles grayscale ramp (232-255)", () => {
      const result = ansiToSegments("\x1b[38;5;240mgray\x1b[0m");
      // 232 = gray 8, each step +10. 240-232=8 steps. gray = 8 + 8*10 = 88
      expect(result[0]?.style).toContain("color:rgb(88,88,88)");
    });

    it("handles background 256-color via 48;5;n", () => {
      const result = ansiToSegments("\x1b[48;5;4mbg\x1b[0m");
      expect(result[0]?.style).toContain("background:#3465a4");
    });
  });

  describe("24-bit true-color mode", () => {
    it("handles foreground 38;2;r;g;b", () => {
      const result = ansiToSegments("\x1b[38;2;100;200;50mtrue\x1b[0m");
      expect(result[0]?.style).toContain("color:rgb(100,200,50)");
    });

    it("handles background 48;2;r;g;b", () => {
      const result = ansiToSegments("\x1b[48;2;10;20;30mbg\x1b[0m");
      expect(result[0]?.style).toContain("background:rgb(10,20,30)");
    });
  });

  it("combines multiple attributes in one segment", () => {
    const result = ansiToSegments("\x1b[1;3;31;42mstyledtext\x1b[0m");
    const style = result[0]?.style ?? "";
    expect(style).toContain("font-weight:bold");
    expect(style).toContain("font-style:italic");
    expect(style).toContain("color:#cc0000");
    expect(style).toContain("background:#00cc00");
  });

  it("handles empty SGR (bare ESC[m) as reset", () => {
    const result = ansiToSegments("\x1b[1mbold\x1b[mnormal");
    // Empty param string means params = [0] (due to Number("") === 0)
    expect(result[1]?.style).toBe("");
  });
});

describe("stripAnsi", () => {
  it("removes ANSI escape sequences", () => {
    expect(stripAnsi("\x1b[31mhello\x1b[0m")).toBe("hello");
  });

  it("returns plain text unchanged", () => {
    expect(stripAnsi("no ansi here")).toBe("no ansi here");
  });

  it("strips multiple escape sequences", () => {
    expect(stripAnsi("\x1b[1;31mred\x1b[0m and \x1b[32mgreen\x1b[0m")).toBe(
      "red and green",
    );
  });

  it("handles empty string", () => {
    expect(stripAnsi("")).toBe("");
  });

  it("strips sequences with no text between them", () => {
    expect(stripAnsi("\x1b[31m\x1b[42m\x1b[0m")).toBe("");
  });
});
