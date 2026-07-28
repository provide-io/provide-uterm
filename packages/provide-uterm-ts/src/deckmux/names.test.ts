//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  ADJECTIVES,
  ANIMALS,
  COLORS,
  generateColor,
  generateInitials,
  generateName,
  hashInt,
  lineToEdgePosition,
  scrollCenterLine,
  viewportToEdgeRange,
} from "./index.ts";

interface NamesGolden {
  adjectives: string[];
  animals: string[];
  colors: string[];
  names: Array<{ connection_id: string; hash: string; name: string; color: string; initials: string }>;
  color_walk: Array<{ name: string; steps_taken: number; taken: string[]; color: string }>;
  initials: Array<{ name: string; display: string; initials: string }>;
  edge: Array<{
    name: string;
    scroll_top_line: number;
    visible_lines: number;
    total_lines: number;
    top_pct: number;
    height_pct: number;
  }>;
  positions: Array<{ name: string; line: number; total_lines: number; position: number }>;
  centers: Array<{ name: string; scroll_top: number; visible_lines: number; center: number }>;
}

const golden = loadGolden<NamesGolden>("deckmux_names_golden.json");

describe("the derivation tables", () => {
  it("are the reference's, in the reference's order", () => {
    // The tables are a wire format: reordering one renames everybody.
    expect(ADJECTIVES).toStrictEqual(golden.adjectives);
    expect(ANIMALS).toStrictEqual(golden.animals);
    expect(COLORS).toStrictEqual(golden.colors);
  });
});

describe("hashing a connection id", () => {
  it.each(golden.names)("$connection_id", (record) => {
    // The full 256-bit digest, not a truncation: the adjective comes from the
    // low bits and the animal from bits 8 and up, so a narrower read renames
    // both halves.
    expect(hashInt(record.connection_id)).toBe(BigInt(record.hash));
  });
});

describe("deriving a name", () => {
  it.each(golden.names)("$connection_id", (record) => {
    expect(generateName(record.connection_id)).toBe(record.name);
  });

  it("is stable across calls", () => {
    // Two servers, or a server and a reconnecting browser, must agree without
    // having spoken to each other.
    expect(generateName("conn-1")).toBe(generateName("conn-1"));
  });

  it("gives different ids different names", () => {
    const names = new Set(golden.names.map((record) => record.name));
    expect(names.size).toBeGreaterThan(1);
  });

  it("names an empty id rather than failing", () => {
    // A connection with no id still has to appear in the participant list.
    const record = golden.names.find((entry) => entry.connection_id === "");
    expect(record?.name).toBeTruthy();
  });
});

describe("choosing a colour", () => {
  it.each(golden.names)("$connection_id", (record) => {
    expect(generateColor(record.connection_id)).toBe(record.color);
  });

  it.each(golden.color_walk)("$name", (record) => {
    expect(generateColor("conn-1", new Set(record.taken))).toBe(record.color);
  });

  it("walks past the colours already in use", () => {
    // Two people the same colour is the failure this avoids.
    const natural = golden.color_walk.find((entry) => entry.steps_taken === 0);
    const next = golden.color_walk.find((entry) => entry.steps_taken === 1);
    expect(next?.color).not.toBe(natural?.color);
    expect(next?.taken).toStrictEqual([natural?.color]);
  });

  it("hands back a duplicate when every colour is taken", () => {
    // Deliberately: somebody with a shared colour still gets to join, where
    // returning nothing would leave them unrenderable.
    const all = golden.color_walk.find((entry) => entry.steps_taken === COLORS.length);
    expect(all?.taken).toHaveLength(COLORS.length);
    expect(COLORS).toContain(all?.color);
    expect(generateColor("conn-1", new Set(COLORS))).toBe(all?.color);
  });

  it("only walks as far as the palette", () => {
    // One short of the whole palette still finds the free one.
    const almost = golden.color_walk.find((entry) => entry.steps_taken === COLORS.length - 1);
    expect(almost?.taken).not.toContain(almost?.color);
  });

  it("defaults to nothing taken", () => {
    expect(generateColor("conn-1")).toBe(generateColor("conn-1", new Set()));
  });
});

describe("initials", () => {
  it.each(golden.initials)("$name", (record) => {
    expect(generateInitials(record.display)).toBe(record.initials);
  });

  it("takes one letter from each of the first two words", () => {
    expect(golden.initials.find((entry) => entry.name === "two words")?.initials).toBe("RF");
    expect(golden.initials.find((entry) => entry.name === "three words")?.initials).toBe("MJ");
  });

  it("falls back to the first two characters of a single word", () => {
    expect(golden.initials.find((entry) => entry.name === "one word")?.initials).toBe("AL");
  });

  it("slices by character, not by storage unit", () => {
    // Half of a surrogate pair is not a character, and would render as a
    // replacement glyph in every participant's avatar.
    expect(golden.initials.find((entry) => entry.name === "two astral characters")?.initials).toBe("😀😁");
    expect(golden.initials.find((entry) => entry.name === "an astral character and a letter")?.initials).toBe("😀X");
  });

  it("copes with a name shorter than the initials", () => {
    expect(golden.initials.find((entry) => entry.name === "one short word")?.initials).toBe("A");
    expect(golden.initials.find((entry) => entry.name === "empty")?.initials).toBe("");
  });

  it("ignores leading whitespace when splitting", () => {
    expect(golden.initials.find((entry) => entry.name === "leading space")?.initials).toBe("BS");
  });

  it("splits on any run of whitespace", () => {
    // A name carrying a tab, a newline or a double space still has two words
    // in it — split on a single space and the second initial comes from the
    // wrong place, or from nowhere.
    expect(golden.initials.find((entry) => entry.name === "a tab between the words")?.initials).toBe("BS");
    expect(golden.initials.find((entry) => entry.name === "two spaces between the words")?.initials).toBe("BS");
    expect(golden.initials.find((entry) => entry.name === "a newline between the words")?.initials).toBe("BS");
    expect(generateInitials("Bob\t\tSmith")).toBe("BS");
  });
});

describe("the edge bar", () => {
  it.each(golden.edge)("$name", (record) => {
    expect(viewportToEdgeRange(record.scroll_top_line, record.visible_lines, record.total_lines)).toStrictEqual([
      record.top_pct,
      record.height_pct,
    ]);
  });

  it("fills the bar when there is nothing to scroll", () => {
    // Zero lines would otherwise divide by zero; a full bar says "all of it".
    const record = golden.edge.find((entry) => entry.name === "no lines at all");
    expect([record?.top_pct, record?.height_pct]).toStrictEqual([0.0, 1.0]);
    expect(golden.edge.find((entry) => entry.name === "a negative total")?.height_pct).toBe(1.0);
  });

  it("never runs the bar past the bottom", () => {
    // The height is clamped by what is left below the top, so a viewport
    // taller than the remaining buffer does not overflow the track.
    for (const record of golden.edge) {
      expect(record.top_pct + record.height_pct).toBeLessThanOrEqual(1.0);
    }
  });

  it("rounds half to even", () => {
    // 1/32 sits exactly on a half at the fourth place. Multiply-round-divide
    // gives 0.0313 here, which is a different pixel.
    expect(golden.edge.find((entry) => entry.name === "exactly on a rounding half")?.top_pct).toBe(0.0312);
    expect(golden.edge.find((entry) => entry.name === "the next half up")?.top_pct).toBe(0.1562);
    expect(golden.edge.find((entry) => entry.name === "a half that rounds the other way")?.top_pct).toBe(0.4062);
  });

  it("rounds a repeating fraction to four places", () => {
    expect(golden.edge.find((entry) => entry.name === "a third")?.top_pct).toBe(0.3333);
    expect(golden.edge.find((entry) => entry.name === "two thirds")?.top_pct).toBe(0.6667);
  });
});

describe("a line's position on the bar", () => {
  it.each(golden.positions)("$name", (record) => {
    expect(lineToEdgePosition(record.line, record.total_lines)).toBe(record.position);
  });

  it("clamps past the end of the buffer", () => {
    // A stale line number from a browser must not point off the track.
    expect(golden.positions.find((entry) => entry.name === "past the end")?.position).toBe(1.0);
  });

  it("sits at the top when there is nothing to scroll", () => {
    expect(golden.positions.find((entry) => entry.name === "no lines at all")?.position).toBe(0.0);
    expect(golden.positions.find((entry) => entry.name === "a negative total")?.position).toBe(0.0);
  });

  it("rounds half to even", () => {
    expect(golden.positions.find((entry) => entry.name === "exactly on a rounding half")?.position).toBe(0.0312);
    expect(golden.positions.find((entry) => entry.name === "a half that rounds the other way")?.position).toBe(0.4062);
  });
});

describe("the centre of a viewport", () => {
  it.each(golden.centers)("$name", (record) => {
    expect(scrollCenterLine(record.scroll_top, record.visible_lines)).toBe(record.center);
  });

  it("floors an odd height", () => {
    // An integer line number: half a line is not somewhere to scroll to.
    expect(golden.centers.find((entry) => entry.name === "an odd height")?.center).toBe(22);
    expect(golden.centers.find((entry) => entry.name === "an even height")?.center).toBe(22);
  });

  it("is the top itself when there is no height", () => {
    expect(golden.centers.find((entry) => entry.name === "no height")?.center).toBe(10);
  });
});
