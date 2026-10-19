//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { CONFORMANCE_DIR } from "../testing/golden.ts";
import { sessionCodec, TerminalEmulator } from "./index.ts";

interface SessionEncodingRecord {
  name: string;
  encoding: string;
  cols: number;
  rows: number;
  reads: string[];
  screen: string;
  cursor: { x: number; y: number };
  ansi_screen: string;
}

// conformance/snapshot/session_encoding.json: the reads a session's emulator
// is fed, its receive encoding, and what the Python emulator holds after them.
const corpus = JSON.parse(readFileSync(join(CONFORMANCE_DIR, "snapshot", "session_encoding.json"), "utf-8")) as {
  records: SessionEncodingRecord[];
};

describe("decoding a session's output", () => {
  it("has records in both encodings", () => {
    expect(new Set(corpus.records.map((record) => record.encoding))).toStrictEqual(new Set(["utf-8", "cp437"]));
    expect(corpus.records.length).toBe(10);
  });

  for (const record of corpus.records) {
    it(`holds what the reference holds: ${record.name}`, () => {
      const emulator = new TerminalEmulator({ cols: record.cols, rows: record.rows, receiveEncoding: record.encoding });
      for (const read of record.reads) {
        emulator.process(new Uint8Array(Buffer.from(read, "base64")));
      }
      const snapshot = emulator.getSnapshot();
      expect([snapshot.screen, snapshot.cursor, emulator.ansiScreen()]).toStrictEqual([
        record.screen,
        record.cursor,
        record.ansi_screen,
      ]);
    });
  }

  it("keeps a byte order mark as text, as Python's UTF-8 codec does", () => {
    const emulator = new TerminalEmulator({ cols: 4, rows: 1, receiveEncoding: "utf-8" });
    emulator.process(new Uint8Array([0xef, 0xbb, 0xbf, 0x61]));
    expect(emulator.getRawTail()).toBe("﻿a");
  });
});

describe("sessionCodec", () => {
  it("knows UTF-8 and CP437 by their common names", () => {
    for (const name of ["utf-8", "UTF-8", "utf8", "utf_8", "U8"]) {
      expect(sessionCodec(name)).toBe("utf-8");
    }
    for (const name of ["cp437", "CP437", "ibm437", "437"]) {
      expect(sessionCodec(name)).toBe("cp437");
    }
  });

  it("refuses any other codec rather than decoding with the wrong one", () => {
    expect(() => sessionCodec("latin-1")).toThrow(new RangeError("unsupported receive encoding: latin-1"));
    expect(() => new TerminalEmulator({ receiveEncoding: "koi8-r" })).toThrow(RangeError);
  });
});
