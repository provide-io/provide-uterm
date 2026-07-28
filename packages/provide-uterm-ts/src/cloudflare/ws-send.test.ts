//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { encodeTerminalData } from "../control-channel/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { frameForWire, isTerminalFrame } from "./index.ts";

interface SendGolden {
  terminal_types: string[];
  frames: Array<{ name: string; payload: Record<string, unknown>; wire: string; terminal: boolean }>;
}

const golden = loadGolden<SendGolden>("wssend_golden.json");

/** The one case the two runtimes stringify differently. */
const NULL_DATA = "terminal output that is null";

describe("framing what a session sends", () => {
  it.each(golden.frames.filter((entry) => entry.name !== NULL_DATA))("$name", (record) => {
    expect(frameForWire(record.payload)).toBe(record.wire);
    expect(isTerminalFrame(record.payload)).toBe(record.terminal);
  });

  it("sends terminal data as terminal data", () => {
    // A browser reads the two differently; a screen update delivered as a
    // control frame renders as nothing at all.
    for (const type of golden.terminal_types) {
      expect(frameForWire({ type, data: "hello" })).toBe(encodeTerminalData("hello"));
    }
  });

  it("sends everything else as a control frame", () => {
    // The safe direction: a control frame a browser does not understand is
    // ignored, where terminal bytes it did not expect are printed.
    for (const type of ["snapshot", "error", "presence_sync", "nonsense"]) {
      expect(frameForWire({ type })).not.toBe(encodeTerminalData(""));
      expect(isTerminalFrame({ type })).toBe(false);
    }
  });

  it("compares the type exactly", () => {
    // A type that merely resembles a terminal one is not one.
    for (const type of ["TERM", " term ", "input_send", "terminal"]) {
      expect(isTerminalFrame({ type, data: "x" })).toBe(false);
    }
  });

  it("treats an unusable type as a control frame", () => {
    for (const type of [undefined, null, "", 7, {}]) {
      expect(isTerminalFrame({ type, data: "x" })).toBe(false);
    }
  });

  it("sends an absent data field as nothing", () => {
    expect(frameForWire({ type: "term" })).toBe(encodeTerminalData(""));
    expect(frameForWire({ type: "term", data: "" })).toBe(encodeTerminalData(""));
  });

  it("carries escape sequences through untouched", () => {
    // The whole point of the terminal channel: what the worker wrote is what
    // the screen gets.
    const escaped = "[31mred[0m";
    expect(frameForWire({ type: "term", data: escaped })).toBe(encodeTerminalData(escaped));
  });

  it("stringifies a data field that is not a string", () => {
    // Rather than refusing: a frame that failed to send would lose the
    // session's output rather than one field of it.
    expect(frameForWire({ type: "term", data: 7 })).toBe(encodeTerminalData("7"));
  });

  it("stringifies a null data field differently from the reference", () => {
    // `str(None)` is "None" and `String(null)` is "null". Recorded rather
    // than papered over — and unreachable from the session itself, which
    // never builds a terminal frame without data.
    expect(frameForWire({ type: "term", data: null })).toBe(encodeTerminalData("null"));
    expect(golden.frames.find((entry) => entry.name === NULL_DATA)?.wire).toBe("None");
  });

  it("ignores the other fields of a terminal frame", () => {
    // Only the data reaches the screen; a sequence number rides on the
    // control channel or not at all.
    expect(frameForWire({ type: "term", data: "x", seq: 1 })).toBe(encodeTerminalData("x"));
  });
});
