//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import {
  _DLE,
  _STX,
  ControlChannelDecoder,
  encodeControlFrame,
  encodeDataFrame,
  encodeWsFrame,
} from "./hijack-codec.js";

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeControlFrame(payload: Record<string, unknown>): string {
  const json = JSON.stringify(payload);
  return `${_DLE}${_STX}${json.length.toString(16).padStart(8, "0")}:${json}`;
}

// ── encodeDataFrame ───────────────────────────────────────────────────────────

describe("encodeDataFrame", () => {
  it("passes through plain text unchanged", () => {
    expect(encodeDataFrame("hello")).toBe("hello");
  });

  it("escapes DLE by doubling it", () => {
    expect(encodeDataFrame("\x10")).toBe("\x10\x10");
  });

  it("escapes multiple DLE characters", () => {
    expect(encodeDataFrame("a\x10b\x10c")).toBe("a\x10\x10b\x10\x10c");
  });

  it("converts null to empty string", () => {
    expect(encodeDataFrame(null)).toBe("");
  });

  it("converts undefined to empty string", () => {
    expect(encodeDataFrame(undefined)).toBe("");
  });

  it("converts numbers to string", () => {
    expect(encodeDataFrame(42)).toBe("42");
  });
});

// ── encodeControlFrame ────────────────────────────────────────────────────────

describe("encodeControlFrame", () => {
  it("produces DLE STX prefix", () => {
    const result = encodeControlFrame({ type: "hello" });
    expect(result.startsWith(_DLE + _STX)).toBe(true);
  });

  it("embeds 8-hex-digit length", () => {
    const json = JSON.stringify({ type: "hello" });
    const result = encodeControlFrame({ type: "hello" });
    const length = Number.parseInt(result.slice(2, 10), 16);
    expect(length).toBe(json.length);
  });

  it("has colon separator after length", () => {
    const result = encodeControlFrame({ type: "x" });
    expect(result[10]).toBe(":");
  });

  it("includes JSON payload", () => {
    const payload = { type: "snapshot", data: "abc" };
    const result = encodeControlFrame(payload);
    const json = result.slice(11);
    expect(JSON.parse(json)).toEqual(payload);
  });

  it("handles empty object", () => {
    const result = encodeControlFrame({});
    expect(result).toBe(`${_DLE}${_STX}00000002:{}`);
  });
});

// ── encodeWsFrame ─────────────────────────────────────────────────────────────

describe("encodeWsFrame", () => {
  it("encodes type=input as data frame (no header)", () => {
    const result = encodeWsFrame({ type: "input", data: "hello" });
    expect(result.startsWith(_DLE + _STX)).toBe(false);
    expect(result).toBe("hello");
  });

  it("encodes type=term as data frame", () => {
    const result = encodeWsFrame({ type: "term", data: "world" });
    expect(result).toBe("world");
  });

  it("encodes type=input with DLE data escape", () => {
    const result = encodeWsFrame({ type: "input", data: "\x10" });
    expect(result).toBe("\x10\x10");
  });

  it("encodes type=input with missing data as empty", () => {
    const result = encodeWsFrame({ type: "input" });
    expect(result).toBe("");
  });

  it("encodes other types as control frame", () => {
    const result = encodeWsFrame({ type: "snapshot_req" });
    expect(result.startsWith(_DLE + _STX)).toBe(true);
  });

  it("encodes hijack_request as control frame", () => {
    const result = encodeWsFrame({ type: "hijack_request" });
    const json = JSON.parse(result.slice(11));
    expect(json).toEqual({ type: "hijack_request" });
  });
});

// ── ControlChannelDecoder ──────────────────────────────────────────────────────

describe("ControlChannelDecoder", () => {
  describe("plain data", () => {
    it("returns a single data frame for plain text", () => {
      const dec = new ControlChannelDecoder();
      const frames = dec.feed("hello world");
      expect(frames).toHaveLength(1);
      expect(frames[0]).toEqual({ type: "data", data: "hello world" });
    });

    it("returns empty array for empty input", () => {
      const dec = new ControlChannelDecoder();
      expect(dec.feed("")).toHaveLength(0);
    });

    it("buffers a trailing lone DLE across multiple feeds", () => {
      // DLE at end of a feed is buffered (incomplete escape sequence)
      const dec = new ControlChannelDecoder();
      expect(dec.feed("abc\x10")).toHaveLength(0); // buffered — trailing DLE
      // Complete the escape on the next feed
      const frames = dec.feed("\x10rest");
      expect(frames).toHaveLength(1);
      expect(frames[0]).toEqual({ type: "data", data: "abc\x10rest" });
    });
  });

  describe("DLE escaping", () => {
    it("unescapes doubled DLE to single DLE in data", () => {
      const dec = new ControlChannelDecoder();
      const frames = dec.feed("\x10\x10");
      expect(frames).toHaveLength(1);
      expect(frames[0]).toEqual({ type: "data", data: "\x10" });
    });

    it("handles DLE escape mid-text", () => {
      const dec = new ControlChannelDecoder();
      const frames = dec.feed("a\x10\x10b");
      expect(frames[0]).toEqual({ type: "data", data: "a\x10b" });
    });

    it("buffers trailing lone DLE (incomplete escape)", () => {
      const dec = new ControlChannelDecoder();
      // Lone DLE at end — incomplete, must buffer
      const frames = dec.feed("abc\x10");
      expect(frames).toHaveLength(0); // nothing emitted yet
      // Complete the escape
      const frames2 = dec.feed("\x10");
      expect(frames2[0]).toEqual({ type: "data", data: "abc\x10" });
    });
  });

  describe("control frames", () => {
    it("decodes a well-formed control frame", () => {
      const dec = new ControlChannelDecoder();
      const frame = makeControlFrame({ type: "hello", worker_id: "w1" });
      const frames = dec.feed(frame);
      expect(frames).toHaveLength(1);
      expect(frames[0]).toEqual({ type: "control", control: { type: "hello", worker_id: "w1" } });
    });

    it("emits data frame before control frame in mixed input", () => {
      const dec = new ControlChannelDecoder();
      const ctrl = makeControlFrame({ type: "ping" });
      const frames = dec.feed(`some text${ctrl}`);
      expect(frames).toHaveLength(2);
      expect(frames[0]).toEqual({ type: "data", data: "some text" });
      expect(frames[1]).toEqual({ type: "control", control: { type: "ping" } });
    });

    it("buffers a partial control frame header", () => {
      const dec = new ControlChannelDecoder();
      const frame = makeControlFrame({ type: "x" });
      // Feed only the DLE+STX prefix (incomplete header)
      const frames = dec.feed(frame.slice(0, 5));
      expect(frames).toHaveLength(0);
      // Complete it
      const frames2 = dec.feed(frame.slice(5));
      expect(frames2).toHaveLength(1);
      expect(frames2[0]).toMatchObject({ type: "control" });
    });

    it("buffers a partial control frame payload", () => {
      const dec = new ControlChannelDecoder();
      const frame = makeControlFrame({ type: "snapshot" });
      // Feed header + partial payload
      const cutpoint = 11 + 5;
      dec.feed(frame.slice(0, cutpoint));
      const frames = dec.feed(frame.slice(cutpoint));
      expect(frames).toHaveLength(1);
      expect(frames[0]).toMatchObject({ type: "control", control: { type: "snapshot" } });
    });

    it("handles multiple consecutive control frames", () => {
      const dec = new ControlChannelDecoder();
      const f1 = makeControlFrame({ type: "a" });
      const f2 = makeControlFrame({ type: "b" });
      const frames = dec.feed(f1 + f2);
      expect(frames).toHaveLength(2);
      expect(frames[0]).toMatchObject({ type: "control", control: { type: "a" } });
      expect(frames[1]).toMatchObject({ type: "control", control: { type: "b" } });
    });
  });

  describe("reset", () => {
    it("clears the internal buffer", () => {
      const dec = new ControlChannelDecoder();
      dec.feed("partial\x10"); // leaves DLE in buffer
      dec.reset();
      // After reset, plain text should decode cleanly
      const frames = dec.feed("clean");
      expect(frames[0]).toEqual({ type: "data", data: "clean" });
    });
  });

  describe("error cases", () => {
    it("throws on invalid control prefix (DLE followed by non-STX non-DLE)", () => {
      const dec = new ControlChannelDecoder();
      expect(() => dec.feed("\x10X")).toThrow("invalid control channel prefix");
    });

    it("throws on non-hex characters in length field", () => {
      const dec = new ControlChannelDecoder();
      // DLE STX + 8 chars that are not valid hex + : + payload
      expect(() => dec.feed(`${_DLE}${_STX}ZZZZZZZZ:x`)).toThrow("invalid control channel length");
    });

    it("throws on missing colon separator", () => {
      const dec = new ControlChannelDecoder();
      // DLE STX + valid 8-hex + non-colon separator
      expect(() => dec.feed(`${_DLE}${_STX}00000001|x`)).toThrow("invalid control channel separator");
    });

    it("throws on payload exceeding max bytes", () => {
      const dec = new ControlChannelDecoder(10);
      // Claim 100 bytes but max is 10
      const bigLenHex = (100).toString(16).padStart(8, "0");
      expect(() => dec.feed(`${_DLE}${_STX}${bigLenHex}:x`)).toThrow("control payload too large");
    });

    it("throws on invalid JSON payload", () => {
      const dec = new ControlChannelDecoder();
      const invalidJson = "not-json";
      const lenHex = invalidJson.length.toString(16).padStart(8, "0");
      expect(() => dec.feed(`${_DLE}${_STX}${lenHex}:${invalidJson}`)).toThrow("invalid control payload");
    });

    it("throws when JSON payload is an array (not an object)", () => {
      const dec = new ControlChannelDecoder();
      const arr = JSON.stringify([1, 2, 3]);
      const lenHex = arr.length.toString(16).padStart(8, "0");
      expect(() => dec.feed(`${_DLE}${_STX}${lenHex}:${arr}`)).toThrow("control payload must be an object");
    });

    it("throws when JSON payload is a primitive", () => {
      const dec = new ControlChannelDecoder();
      const num = "42";
      const lenHex = num.length.toString(16).padStart(8, "0");
      expect(() => dec.feed(`${_DLE}${_STX}${lenHex}:${num}`)).toThrow("control payload must be an object");
    });

    it("throws when JSON payload is null", () => {
      const dec = new ControlChannelDecoder();
      const nil = "null";
      const lenHex = nil.length.toString(16).padStart(8, "0");
      expect(() => dec.feed(`${_DLE}${_STX}${lenHex}:${nil}`)).toThrow("control payload must be an object");
    });
  });

  describe("round-trip with encode functions", () => {
    it("round-trips a control frame", () => {
      const payload = { type: "snapshot", screen: "hello\nworld", cols: 80 };
      const encoded = encodeControlFrame(payload);
      const dec = new ControlChannelDecoder();
      const frames = dec.feed(encoded);
      expect(frames).toHaveLength(1);
      expect(frames[0]).toMatchObject({ type: "control", control: payload });
    });

    it("round-trips a data frame with DLE", () => {
      const text = "data with \x10 DLE";
      const encoded = encodeDataFrame(text);
      const dec = new ControlChannelDecoder();
      const frames = dec.feed(encoded);
      expect(frames).toHaveLength(1);
      expect(frames[0]).toEqual({ type: "data", data: text });
    });

    it("round-trips mixed data and control", () => {
      const text = "terminal output";
      const ctrl = { type: "hijack_state", hijacked: true };
      const encoded = encodeDataFrame(text) + encodeControlFrame(ctrl);
      const dec = new ControlChannelDecoder();
      const frames = dec.feed(encoded);
      expect(frames).toHaveLength(2);
      expect(frames[0]).toEqual({ type: "data", data: text });
      expect(frames[1]).toMatchObject({ type: "control", control: ctrl });
    });
  });
});
