//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { DEFAULT_CAPTURE_MAX_CHARS, TerminalCapture } from "./index.ts";

interface CaptureGolden {
  default_max_chars: number;
  captures: Array<{ name: string; max_chars: number; chunks: string[]; text: string }>;
}

const golden = loadGolden<CaptureGolden>("capture_golden.json");

describe("TerminalCapture", () => {
  it.each(golden.captures)("$name", (record) => {
    const capture = new TerminalCapture(record.max_chars);
    for (const chunk of record.chunks) {
      capture.append(chunk);
    }
    expect(capture.text).toBe(record.text);
  });

  it("keeps the tail rather than the head", () => {
    // The right end: a caller capturing output wants what the command
    // finished saying, not the banner it started with. It also means the
    // capture is a sliding window, not a prefix of the real output.
    const record = golden.captures.find((entry) => entry.name === "one past the bound");
    expect(record?.text).toBe("bcdefghi");
  });

  it("counts by character, not by UTF-16 unit", () => {
    // Python slices by code point. A naive slice would cut an astral
    // character in half and, worse, keep a different number of them.
    const record = golden.captures.find((entry) => entry.name === "astral characters");
    const capture = new TerminalCapture(4);
    capture.append("a\u{1F600}b\u{1F600}c");
    expect(capture.text).toBe(record?.text);
    expect([...capture.text]).toHaveLength(4);
  });

  it("never leaves a lone surrogate", () => {
    // Half an emoji is not a character, and it corrupts anything that
    // re-encodes the captured text.
    const capture = new TerminalCapture(3);
    capture.append("\u{1F600}\u{1F600}\u{1F600}\u{1F600}");
    expect(capture.text).toBe("\u{1F600}\u{1F600}\u{1F600}");
    for (const char of capture.text) {
      expect(char.codePointAt(0)).toBeGreaterThan(0xffff);
    }
  });

  it("raises a bound below one", () => {
    // A capture that can hold nothing would silently discard everything it
    // was asked to record.
    for (const name of ["bound of zero is raised to one", "negative bound is raised to one"]) {
      expect(golden.captures.find((entry) => entry.name === name)?.text).toBe("c");
    }
    expect(new TerminalCapture(0).limit).toBe(1);
    expect(new TerminalCapture(-5).limit).toBe(1);
  });

  it("truncates a fractional bound toward zero", () => {
    expect(new TerminalCapture(8.9).limit).toBe(8);
  });

  it("uses the reference default bound", () => {
    expect(DEFAULT_CAPTURE_MAX_CHARS).toBe(golden.default_max_chars);
    expect(new TerminalCapture().limit).toBe(DEFAULT_CAPTURE_MAX_CHARS);
  });

  it("starts empty", () => {
    expect(new TerminalCapture(16).text).toBe("");
  });

  it("ignores an empty chunk", () => {
    const capture = new TerminalCapture(4);
    capture.append("ab");
    capture.append("");
    expect(capture.text).toBe("ab");
  });
});
