//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { escapeIac, parseTelnetBuffer, type TelnetEvent } from "./index.ts";

interface RecordedEvent {
  is_sub: boolean;
  cmd: number;
  opt: number;
  body: number[];
}

interface TelnetGolden {
  escapes: Array<{ name: string; input: number[]; output: number[] }>;
  parses: Array<{
    name: string;
    input: number[];
    final: boolean;
    payload: number[];
    events: RecordedEvent[];
    consumed: number;
  }>;
  stream: number[];
  stream_in_pieces: Record<string, { payload: number[]; events: RecordedEvent[]; leftover: number[] }>;
}

const golden = loadGolden<TelnetGolden>("telnetparse_golden.json");

const IAC = 255;
const SB = 250;
const SE = 240;
const DO = 253;

/** Events in the shape the corpus records. */
function recorded(events: TelnetEvent[]): RecordedEvent[] {
  return events.map((event) => ({ is_sub: event.isSub, cmd: event.cmd, opt: event.opt, body: [...event.body] }));
}

describe("escaping a command byte", () => {
  it.each(golden.escapes)("$name", (record) => {
    expect([...escapeIac(Uint8Array.from(record.input))]).toEqual(record.output);
  });

  it("doubles a command byte and nothing else", () => {
    // Without this, a byte the application meant literally is read as the
    // start of a negotiation by whatever is upstream.
    expect([...escapeIac(Uint8Array.from([97, IAC, 98]))]).toEqual([97, IAC, IAC, 98]);
    expect([...escapeIac(Uint8Array.from([97, 98]))]).toEqual([97, 98]);
  });

  it("doubles each of two command bytes", () => {
    expect([...escapeIac(Uint8Array.from([IAC, IAC]))]).toEqual([IAC, IAC, IAC, IAC]);
  });
});

describe("parsing a buffer", () => {
  it.each(golden.parses)("$name", (record) => {
    const result = parseTelnetBuffer(Uint8Array.from(record.input), record.final);
    expect([...result.payload]).toEqual(record.payload);
    expect(recorded(result.events)).toEqual(record.events);
    expect(result.consumed).toBe(record.consumed);
  });

  it("separates payload from negotiation", () => {
    const result = parseTelnetBuffer(Uint8Array.from([104, 105, IAC, DO, 24]));
    expect(Buffer.from(result.payload).toString()).toBe("hi");
    expect(recorded(result.events)).toEqual([{ is_sub: false, cmd: DO, opt: 24, body: [] }]);
  });

  it("reads a doubled command byte as one literal", () => {
    // How a payload byte of 255 is carried; a parser missing it would read
    // the second as the start of a command.
    const result = parseTelnetBuffer(Uint8Array.from([97, IAC, IAC, 98]));
    expect([...result.payload]).toEqual([97, IAC, 98]);
    expect(result.events).toEqual([]);
  });

  it("lifts a subnegotiation out with its option and body", () => {
    const result = parseTelnetBuffer(Uint8Array.from([IAC, SB, 24, 1, 2, IAC, SE]));
    expect(recorded(result.events)).toEqual([{ is_sub: true, cmd: SB, opt: 24, body: [1, 2] }]);
    expect([...result.payload]).toEqual([]);
  });

  it("reports no option for a subnegotiation with no body at all", () => {
    expect(parseTelnetBuffer(Uint8Array.from([IAC, SB, IAC, SE])).events[0]?.opt).toBe(0);
  });

  it("skips a command it does not know without eating text", () => {
    const result = parseTelnetBuffer(Uint8Array.from([IAC, 99, 116, 101, 120, 116]));
    expect(Buffer.from(result.payload).toString()).toBe("text");
  });
});

describe("a command split across two reads", () => {
  it("does not consume an incomplete one", () => {
    // It stays in the buffer until the bytes that finish it arrive — which is
    // why `consumed` is reported separately, since a parser that consumed a
    // half-read command would lose it.
    for (const input of [
      [104, 105, IAC],
      [104, 105, IAC, DO],
      [104, 105, IAC, SB, 24, 1],
    ]) {
      const result = parseTelnetBuffer(Uint8Array.from(input));
      expect(result.consumed).toBe(2);
      expect(Buffer.from(result.payload).toString()).toBe("hi");
    }
  });

  it("emits a trailing partial command once the stream has ended", () => {
    // Half a negotiation is not worth losing the text before it.
    const result = parseTelnetBuffer(Uint8Array.from([104, 105, IAC, DO]), true);
    expect([...result.payload]).toEqual([104, 105, IAC, DO]);
    expect(result.consumed).toBe(4);
  });

  it("holds a doubled byte apart from its pair", () => {
    const result = parseTelnetBuffer(Uint8Array.from([97, IAC]));
    expect([...result.payload]).toEqual([97]);
    expect(result.consumed).toBe(1);
  });

  it("waits for a subnegotiation's terminator", () => {
    // Its end is itself a two-byte sequence, so a buffer ending on the first
    // half has not seen it.
    const result = parseTelnetBuffer(Uint8Array.from([IAC, SB, 24, IAC]));
    expect(result.consumed).toBe(0);
    expect(result.events).toEqual([]);
  });
});

describe("a stream delivered in pieces", () => {
  it.each(Object.keys(golden.stream_in_pieces))("read %s bytes at a time", (size) => {
    // The network chooses the read sizes, so the same stream has to parse the
    // same way whatever they are.
    const expected = golden.stream_in_pieces[size] as (typeof golden.stream_in_pieces)[string];
    const stream = Uint8Array.from(golden.stream);
    const step = Number(size);

    let buffer = new Uint8Array(0);
    const payload: number[] = [];
    const events: RecordedEvent[] = [];
    for (let start = 0; start < stream.length; start += step) {
      const chunk = stream.subarray(start, start + step);
      const next = new Uint8Array(buffer.length + chunk.length);
      next.set(buffer);
      next.set(chunk, buffer.length);
      buffer = next;

      const result = parseTelnetBuffer(buffer, start + step >= stream.length);
      payload.push(...result.payload);
      events.push(...recorded(result.events));
      buffer = buffer.subarray(result.consumed);
    }

    expect(payload).toEqual(expected.payload);
    expect(events).toEqual(expected.events);
    expect([...buffer]).toEqual(expected.leftover);
  });

  it("gives the same answer however it was split", () => {
    const sizes = Object.keys(golden.stream_in_pieces);
    const first = golden.stream_in_pieces[sizes[0] as string];
    for (const size of sizes) {
      expect(golden.stream_in_pieces[size]?.payload).toEqual(first?.payload);
      expect(golden.stream_in_pieces[size]?.events).toEqual(first?.events);
    }
  });
});
