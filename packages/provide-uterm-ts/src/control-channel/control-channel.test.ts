//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  ControlFrameDecoder,
  ControlFrameProtocolError,
  DLE,
  STX,
  encodeControlFrame,
  encodeTerminalData,
  isControlFrame,
} from "./index.ts";
import type { ControlFrameChunk } from "./index.ts";

interface ControlChannelGolden {
  encode_data: Array<{ data: string; out: string }>;
  encode_control: Array<{ payload: Record<string, unknown>; out: string }>;
  float_divergences: Array<{ payload: Record<string, unknown>; cpython: string }>;
  is_control_frame: Array<{ message: string; out: boolean }>;
  decode: Array<{
    name: string;
    chunks: string[];
    events: Array<Record<string, unknown>>;
    error: string | null;
    on_error: string[];
  }>;
  reject: Array<{
    name: string;
    chunks: string[];
    finish: boolean;
    events: Array<Record<string, unknown>>;
    error: string | null;
    on_error: string[];
  }>;
}

const golden = loadGolden<ControlChannelGolden>("control_channel_golden.json");

/** Render a decoded event the way the generator recorded it. */
function toRecord(event: ControlFrameChunk): Record<string, unknown> {
  return event.kind === "data" ? { kind: "data", data: event.data } : { kind: "control", control: event.control };
}

/** Drive a fresh decoder over `chunks` and record what it emitted or threw. */
function drive(
  chunks: readonly string[],
  finish: boolean,
): { events: Array<Record<string, unknown>>; error: string | null; onError: string[] } {
  const onError: string[] = [];
  const decoder = new ControlFrameDecoder({ onError: (code) => onError.push(code) });
  const events: Array<Record<string, unknown>> = [];
  try {
    for (const chunk of chunks) {
      for (const event of decoder.feed(chunk)) {
        events.push(toRecord(event));
      }
    }
    if (finish) {
      for (const event of decoder.finish()) {
        events.push(toRecord(event));
      }
    }
  } catch (error) {
    if (!(error instanceof ControlFrameProtocolError)) {
      throw error;
    }
    return { events, error: error.message, onError };
  }
  return { events, error: null, onError };
}

describe("encodeTerminalData", () => {
  it("passes data with no DLE through unchanged", () => {
    expect(encodeTerminalData("plain terminal output")).toBe("plain terminal output");
  });

  it("doubles every DLE so the decoder cannot mistake it for a frame", () => {
    expect(encodeTerminalData(DLE)).toBe(DLE + DLE);
    expect(encodeTerminalData(`a${DLE}b${DLE}c`)).toBe(`a${DLE}${DLE}b${DLE}${DLE}c`);
  });

  it("escapes a DLE STX pair in data so it cannot open a frame", () => {
    expect(encodeTerminalData(DLE + STX)).toBe(DLE + DLE + STX);
  });
});

describe("encodeControlFrame", () => {
  it("emits the magic bytes, an eight-digit length and a colon", () => {
    expect(encodeControlFrame({ type: "hello" })).toBe(`${DLE}${STX}00000010:{"type":"hello"}`);
  });

  it("counts UTF-8 bytes in the header, not characters", () => {
    // {"t":"你好"} is 10 characters but 14 UTF-8 bytes.
    const frame = encodeControlFrame({ t: "你好" });
    expect(frame.slice(2, 10)).toBe("0000000e");
    expect(frame.slice(11)).toBe('{"t":"你好"}');
  });

  it("serialises compactly, with no whitespace between tokens", () => {
    expect(encodeControlFrame({ a: 1, b: [2, 3] })).toBe(`${DLE}${STX}00000011:{"a":1,"b":[2,3]}`);
  });

  it("round-trips through the structural predicate", () => {
    expect(isControlFrame(encodeControlFrame({ type: "hello" }))).toBe(true);
  });
});

describe("isControlFrame", () => {
  it("accepts a well-formed frame", () => {
    expect(isControlFrame(encodeControlFrame({ type: "hello" }))).toBe(true);
  });

  it("rejects anything shorter than a header", () => {
    expect(isControlFrame("")).toBe(false);
    expect(isControlFrame(`${DLE}${STX}0000000`)).toBe(false);
  });

  it("rejects the wrong magic bytes", () => {
    expect(isControlFrame(`\x11${STX}00000002:{}`)).toBe(false);
    expect(isControlFrame(`${DLE}\x0300000002:{}`)).toBe(false);
  });

  it("rejects a missing separator", () => {
    expect(isControlFrame(`${DLE}${STX}00000002;{}`)).toBe(false);
  });

  it("rejects a non-hex length", () => {
    expect(isControlFrame(`${DLE}${STX}0000000g:{}`)).toBe(false);
  });

  it("rejects an uppercase length, which is not the canonical form", () => {
    expect(isControlFrame(`${DLE}${STX}0000000A:{"a":"aaaaaaaaa"}`)).toBe(false);
  });

  it("rejects a length above the one-mebibyte ceiling", () => {
    expect(isControlFrame(`${DLE}${STX}00100001:{}`)).toBe(false);
  });

  it("rejects a frame whose payload is incomplete", () => {
    expect(isControlFrame(`${DLE}${STX}000000ff:{}`)).toBe(false);
  });

  it("rejects trailing bytes after a complete frame", () => {
    expect(isControlFrame(`${encodeControlFrame({ type: "hello" })}x`)).toBe(false);
  });

  it("rejects a declared length that splits a code point", () => {
    expect(isControlFrame(`${DLE}${STX}00000001:é`)).toBe(false);
  });

  it("checks structure only, not that the payload is JSON", () => {
    expect(isControlFrame(`${DLE}${STX}00000003:abc`)).toBe(true);
  });
});

describe("ControlFrameDecoder", () => {
  it("emits nothing for an empty stream", () => {
    expect(drive([], true)).toStrictEqual({ events: [], error: null, onError: [] });
  });

  it("emits plain data as it arrives, one chunk at a time", () => {
    expect(drive(["one", "two"], true).events).toStrictEqual([
      { kind: "data", data: "one" },
      { kind: "data", data: "two" },
    ]);
  });

  it("unescapes a doubled DLE back to a single literal", () => {
    expect(drive([`a${DLE}${DLE}b`], true).events).toStrictEqual([{ kind: "data", data: `a${DLE}b` }]);
  });

  it("reassembles a control frame split across every byte boundary", () => {
    const frame = encodeControlFrame({ type: "hello", n: 1 });
    expect(drive([...frame], true).events).toStrictEqual([{ kind: "control", control: { type: "hello", n: 1 } }]);
  });

  it("separates data before and after a frame", () => {
    const frame = encodeControlFrame({ type: "x" });
    expect(drive([`a${frame}b`], true).events).toStrictEqual([
      { kind: "data", data: "a" },
      { kind: "control", control: { type: "x" } },
      { kind: "data", data: "b" },
    ]);
  });

  it("decodes two adjacent frames", () => {
    const stream = encodeControlFrame({ a: 1 }) + encodeControlFrame({ b: 2 });
    expect(drive([stream], true).events).toStrictEqual([
      { kind: "control", control: { a: 1 } },
      { kind: "control", control: { b: 2 } },
    ]);
  });

  it("buffers a trailing lone DLE rather than emitting it", () => {
    const decoder = new ControlFrameDecoder();
    expect(decoder.feed(`data${DLE}`)).toStrictEqual([{ kind: "data", data: "data" }]);
    expect(() => decoder.finish()).toThrow(ControlFrameProtocolError);
  });

  it("rejects a DLE followed by neither DLE nor STX", () => {
    expect(() => new ControlFrameDecoder().feed(`a${DLE}x`)).toThrow(/invalid control prefix/);
  });

  it("rejects a payload that is not a JSON object", () => {
    expect(() => new ControlFrameDecoder().feed(`${DLE}${STX}00000002:[]`)).toThrow(
      /control payload must be an object/,
    );
  });

  it("rejects nesting deeper than the configured depth", () => {
    const body = `${"[".repeat(40)}1${"]".repeat(40)}`;
    const serialized = `{"d":${body}}`;
    const frame = `${DLE}${STX}${Buffer.byteLength(serialized, "utf-8").toString(16).padStart(8, "0")}:${serialized}`;
    expect(() => new ControlFrameDecoder().feed(frame)).toThrow(/nests deeper than 32/);
  });

  it("reports a buffer overflow and drops the buffer", () => {
    const decoder = new ControlFrameDecoder({ maxBufferBytes: 8 });
    expect(() => decoder.feed("123456789")).toThrow(/control frame buffer overflow: 9 > 8/);
    // The buffer was dropped, so a fresh well-formed stream still decodes.
    expect(decoder.feed("ok")).toStrictEqual([{ kind: "data", data: "ok" }]);
  });

  it("enforces a caller-supplied payload ceiling below the protocol ceiling", () => {
    const decoder = new ControlFrameDecoder({ maxControlPayloadBytes: 4 });
    expect(() => decoder.feed(`${DLE}${STX}00000010:{"type":"hello"}`)).toThrow(/control payload too large/);
  });

  it("fires the error hook exactly once per rejection", () => {
    const codes: string[] = [];
    const decoder = new ControlFrameDecoder({ onError: (code) => codes.push(code) });
    expect(() => decoder.feed(`${DLE}${STX}00000003:abc`)).toThrow(ControlFrameProtocolError);
    expect(codes).toStrictEqual(["control_frame_protocol_error"]);
  });

  it("clears its buffer after an error so the next feed starts clean", () => {
    const decoder = new ControlFrameDecoder();
    expect(() => decoder.feed(`a${DLE}x`)).toThrow(ControlFrameProtocolError);
    expect(decoder.feed("fresh")).toStrictEqual([{ kind: "data", data: "fresh" }]);
  });

  it("rejects a non-string chunk", () => {
    const decoder = new ControlFrameDecoder();
    expect(() => decoder.feed(new Uint8Array([1]) as unknown as string)).toThrow(TypeError);
  });
});

describe("differential parity with CPython", () => {
  it("matches every terminal-data encoding record", () => {
    for (const record of golden.encode_data) {
      expect(encodeTerminalData(record.data)).toBe(record.out);
    }
    expect(golden.encode_data.length).toBeGreaterThan(8);
  });

  it("matches every control-frame encoding record byte-for-byte", () => {
    for (const record of golden.encode_control) {
      expect(encodeControlFrame(record.payload)).toBe(record.out);
    }
    expect(golden.encode_control.length).toBeGreaterThan(15);
  });

  it("documents where JSON float rendering diverges from CPython", () => {
    // Python is the only one of the four implementations that keeps an
    // int/float distinction through JSON: it writes 0.0 where Go's
    // encoding/json, .NET's System.Text.Json and JSON.stringify all write 0.
    // The general control-frame encoder tolerates this, as the Go and C#
    // ports do. The canonical-JSON path used for HMAC identity signatures
    // does not, and reproduces CPython's float repr explicitly.
    const divergences = golden.float_divergences.map((record) => ({
      cpython: record.cpython.slice(11),
      host: encodeControlFrame(record.payload).slice(11),
    }));
    expect(divergences).toStrictEqual([
      { cpython: '{"zero":0.0}', host: '{"zero":0}' },
      { cpython: '{"one":1.0}', host: '{"one":1}' },
      { cpython: '{"negative_zero":-0.0}', host: '{"negative_zero":0}' },
      { cpython: '{"mixed":[1.0,1.5,2]}', host: '{"mixed":[1,1.5,2]}' },
    ]);
  });

  it("matches every structural-predicate record", () => {
    for (const record of golden.is_control_frame) {
      expect({ message: record.message, out: isControlFrame(record.message) }).toStrictEqual({
        message: record.message,
        out: record.out,
      });
    }
    expect(golden.is_control_frame.length).toBeGreaterThan(15);
  });

  it("matches every decode record, including the chunk boundaries", () => {
    for (const record of golden.decode) {
      const actual = drive(record.chunks, true);
      expect({ name: record.name, ...actual }).toStrictEqual({
        name: record.name,
        events: record.events.map((event) =>
          event.kind === "data" ? { kind: "data", data: event.data } : { kind: "control", control: event.control },
        ),
        error: record.error,
        onError: record.on_error,
      });
    }
    expect(golden.decode.length).toBeGreaterThan(15);
  });

  it("matches every rejection record, error text included", () => {
    for (const record of golden.reject) {
      const actual = drive(record.chunks, record.finish);
      expect({ name: record.name, error: actual.error, onError: actual.onError }).toStrictEqual({
        name: record.name,
        error: record.error,
        onError: record.on_error,
      });
    }
    expect(golden.reject.length).toBeGreaterThan(10);
  });
});
