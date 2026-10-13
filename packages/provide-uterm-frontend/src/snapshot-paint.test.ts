//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { SNAPSHOT_PREAMBLE, snapshotCursorRestore, snapshotRows } from "./snapshot-paint.js";

describe("snapshot painting", () => {
  it("resets, clears and homes before the rows", () => {
    expect(SNAPSHOT_PREAMBLE).toBe("\x1b[!p\x1b[2J\x1b[H");
  });

  it("turns the bare row separators into CRLF", () => {
    expect(snapshotRows("ab\ncd\n")).toBe("ab\r\ncd\r\n");
  });

  it("places the cursor of a plain-text screen from the frame, 1-based", () => {
    expect(snapshotCursorRestore("top\n   mid", { x: 3, y: 1 })).toBe("\x1b[2;4H");
    expect(snapshotCursorRestore("", { x: 0, y: 0 })).toBe("\x1b[1;1H");
  });

  it("leaves a deferred-wrap column to the terminal's clamp", () => {
    expect(snapshotCursorRestore("abcd", { x: 4, y: 0 })).toBe("\x1b[1;5H");
  });

  it("does not move the cursor a screen with escape sequences already placed", () => {
    // An emulator's ANSI screen ends with its own cursor, pen and modes; a raw
    // output replay leaves its own cursor. Moving it would undo either.
    expect(snapshotCursorRestore("\x1b[0mrow\x1b[0m\x1b[r\x1b[3;7H", { x: 0, y: 0 })).toBe("");
  });

  it("ignores a frame without a usable cursor", () => {
    expect(snapshotCursorRestore("text", undefined)).toBe("");
    expect(snapshotCursorRestore("text", null)).toBe("");
    expect(snapshotCursorRestore("text", "1,2")).toBe("");
    // The PTY connector names its cursor row/col, not x/y.
    expect(snapshotCursorRestore("text", { row: 1, col: 2 })).toBe("");
    expect(snapshotCursorRestore("text", { x: 1 })).toBe("");
    expect(snapshotCursorRestore("text", { x: -1, y: 0 })).toBe("");
    expect(snapshotCursorRestore("text", { x: 1, y: 0.5 })).toBe("");
    expect(snapshotCursorRestore("text", { x: "1", y: 0 })).toBe("");
  });
});
