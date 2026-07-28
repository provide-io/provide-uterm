//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { ansiToSegments, SEGMENT_COLOR_NAMES, type Segment, tokensToSegments } from "./index.ts";

interface RecordedSegment {
  text: string;
  color: string | null;
  bold: boolean;
}

interface SegmentsGolden {
  color_names: string[];
  ansi: Array<{ name: string; input: string; segments: RecordedSegment[] }>;
  tokens: Array<{ name: string; input: string; segments: RecordedSegment[] }>;
}

const golden = loadGolden<SegmentsGolden>("segments_golden.json");
const ESC = "\x1b";

/** Segments in the shape the corpus records. */
function recorded(segments: Segment[]): RecordedSegment[] {
  return segments.map((segment) => ({ text: segment.text, color: segment.color ?? null, bold: segment.bold }));
}

describe("parsing coloured text into segments", () => {
  it.each(golden.ansi)("$name", (record) => {
    expect(recorded(ansiToSegments(record.input))).toEqual(record.segments);
  });

  it("names the colours a client can theme", () => {
    expect([...SEGMENT_COLOR_NAMES]).toEqual(golden.color_names);
  });

  it("carries plain text through untouched", () => {
    expect(ansiToSegments("hello")).toEqual([{ text: "hello", color: undefined, bold: false }]);
    expect(ansiToSegments("")).toEqual([]);
  });

  it("merges adjacent runs of the same style", () => {
    // A client rendering one span per segment would otherwise emit a span per
    // escape sequence.
    expect(ansiToSegments(`${ESC}[31mred${ESC}[31mmore`)).toEqual([{ text: "redmore", color: "red", bold: false }]);
    expect(ansiToSegments(`plain${ESC}[0mmore`)).toEqual([{ text: "plainmore", color: undefined, bold: false }]);
  });

  it("drops a run with no text in it", () => {
    // An empty span for every reset that changed nothing.
    expect(ansiToSegments(`${ESC}[31m${ESC}[32mgreen`)).toEqual([{ text: "green", color: "green", bold: false }]);
    expect(ansiToSegments(`text${ESC}[0m`)).toEqual([{ text: "text", color: undefined, bold: false }]);
  });

  it("reads a bright colour as its base plus bold", () => {
    // So a client with no separate bright palette still tells the two apart.
    expect(ansiToSegments(`${ESC}[91mbright`)).toEqual([{ text: "bright", color: "red", bold: true }]);
  });

  it("resets both colour and weight", () => {
    expect(ansiToSegments(`${ESC}[1;31mboth${ESC}[0mplain`)).toEqual([
      { text: "both", color: "red", bold: true },
      { text: "plain", color: undefined, bold: false },
    ]);
  });

  it("turns weight off without touching the colour", () => {
    expect(ansiToSegments(`${ESC}[1;31mboth${ESC}[22mthin`)).toEqual([
      { text: "both", color: "red", bold: true },
      { text: "thin", color: "red", bold: false },
    ]);
  });

  it("returns to the default colour without touching the weight", () => {
    expect(ansiToSegments(`${ESC}[1;31mboth${ESC}[39mplain`)).toEqual([
      { text: "both", color: "red", bold: true },
      { text: "plain", color: undefined, bold: true },
    ]);
  });

  it("skips an extended colour's operands rather than reading them", () => {
    // `38;5;196` is one instruction. A parser that walked into it would read
    // the 196 as another code and paint the text a colour nobody asked for.
    expect(ansiToSegments(`${ESC}[38;5;196mtext`)).toEqual([{ text: "text", color: undefined, bold: false }]);
    expect(ansiToSegments(`${ESC}[38;2;255;0;0mtext`)).toEqual([{ text: "text", color: undefined, bold: false }]);
  });

  it("reads what follows an extended colour", () => {
    // Only its operands are skipped, not the rest of the sequence.
    expect(ansiToSegments(`${ESC}[38;5;196;1mtext`)).toEqual([{ text: "text", color: undefined, bold: true }]);
  });

  it("leaves the colour alone through an extended one", () => {
    // There is no semantic name for an extended colour, so the reference does
    // not invent one — the running colour simply stays as it was.
    expect(ansiToSegments(`${ESC}[31m${ESC}[38;5;196mtext`)).toEqual([{ text: "text", color: "red", bold: false }]);
  });

  it("drops an escape that carries no text", () => {
    expect(ansiToSegments(`${ESC}[2Jcleared`)).toEqual([{ text: "cleared", color: undefined, bold: false }]);
    expect(ansiToSegments(`${ESC}[31mred${ESC}[1;1Hmore`)).toEqual([{ text: "redmore", color: "red", bold: false }]);
  });

  it("survives a lone escape", () => {
    expect(ansiToSegments(ESC)).toEqual([]);
    expect(ansiToSegments(`${ESC}text`)).toEqual([{ text: "ext", color: undefined, bold: false }]);
  });

  it("treats an empty parameter list as a reset", () => {
    expect(ansiToSegments(`${ESC}[31mred${ESC}[mplain`)).toEqual([
      { text: "red", color: "red", bold: false },
      { text: "plain", color: undefined, bold: false },
    ]);
  });

  it("treats an empty parameter as zero", () => {
    expect(ansiToSegments(`${ESC}[;31mred`)).toEqual([{ text: "red", color: "red", bold: false }]);
  });

  it("ignores a code it does not model", () => {
    // Underline, a background colour, anything unassigned: the segment stream
    // carries foreground and weight, and nothing else pretends otherwise.
    for (const code of [4, 41, 99]) {
      expect(ansiToSegments(`${ESC}[${code}mtext`)).toEqual([{ text: "text", color: undefined, bold: false }]);
    }
  });
});

describe("rendering the token dialect", () => {
  it.each(golden.tokens)("$name", (record) => {
    expect(recorded(tokensToSegments(record.input))).toEqual(record.segments);
  });

  it("derives its colours from the same ANSI the terminal renders", () => {
    // Which is what stops the two presentations drifting. The expected
    // segment comes from the corpus rather than from a guess at which SGR
    // code the dialect emits — guessing that is exactly the drift this is
    // meant to rule out.
    const recordedTokens = golden.tokens.find((entry) => entry.name === "a token colour");
    expect(recorded(tokensToSegments("{+g}green{-x}"))).toEqual(recordedTokens?.segments);
    expect(recordedTokens?.segments[0]?.color).toBe("green");
  });

  it("carries text with no tokens through untouched", () => {
    expect(tokensToSegments("no tokens here")).toEqual([{ text: "no tokens here", color: undefined, bold: false }]);
  });
});
