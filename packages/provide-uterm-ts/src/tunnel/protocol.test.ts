//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  CHANNEL_CONTROL,
  CHANNEL_DATA,
  CHANNEL_HTTP,
  CHANNEL_TCP,
  decodeControl,
  decodeFrame,
  encodeControl,
  encodeFrame,
  FLAG_DATA,
  FLAG_EOF,
  isControl,
  isEof,
  TunnelProtocolError,
} from "./index.ts";

interface ProtocolGolden {
  protocol: {
    channels: { control: number; data: number; tcp: number; http: number };
    flags: { data: number; eof: number };
    encoded: Array<{
      name: string;
      channel: number;
      payload: string;
      flags: number;
      value?: string;
      error?: string;
    }>;
    decoded: Array<{
      name: string;
      raw: string;
      channel?: number;
      flags?: number;
      payload?: string;
      is_eof?: boolean;
      is_control?: boolean;
      error?: string;
    }>;
    control_encoded: Array<{ name: string; message: Record<string, unknown>; value?: string; error?: string }>;
    control_decoded: Array<{ name: string; payload: string; value?: Record<string, unknown>; error?: string }>;
  };
}

const golden = loadGolden<ProtocolGolden>("share_golden.json").protocol;

/** Latin-1 is how the corpus carries arbitrary bytes through JSON. */
function fromLatin1(text: string): Uint8Array {
  return Uint8Array.from([...text].map((character) => character.charCodeAt(0)));
}

/** The same bytes back out. */
function toLatin1(bytes: Uint8Array): string {
  return String.fromCharCode(...bytes);
}

function fromHex(hex: string): Uint8Array {
  return Uint8Array.from((hex.match(/../g) ?? []).map((byte) => Number.parseInt(byte, 16)));
}

function toHex(bytes: Uint8Array): string {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

describe("the channels and flags themselves", () => {
  it("numbers them as the wire does", () => {
    expect({ control: CHANNEL_CONTROL, data: CHANNEL_DATA, tcp: CHANNEL_TCP, http: CHANNEL_HTTP }).toEqual(
      golden.channels,
    );
    expect({ data: FLAG_DATA, eof: FLAG_EOF }).toEqual(golden.flags);
  });

  it("puts control on zero, so an unset channel is not terminal bytes", () => {
    expect(CHANNEL_CONTROL).toBe(0);
    expect(CHANNEL_DATA).not.toBe(0);
  });
});

describe("encoding a frame", () => {
  it.each(golden.encoded)("$name", (record) => {
    const encode = () => encodeFrame(record.channel, fromLatin1(record.payload), record.flags);
    if (record.error !== undefined) {
      expect(encode).toThrow(record.error);
      expect(encode).toThrow(TunnelProtocolError);
      return;
    }
    expect(toHex(encode())).toBe(record.value);
  });

  it("writes the channel, the flags, then the payload", () => {
    expect([...encodeFrame(CHANNEL_DATA, new TextEncoder().encode("hi"))]).toEqual([1, 0, 104, 105]);
  });

  it("refuses a header value that does not fit in its byte", () => {
    // Which would otherwise be truncated into a different channel — a frame
    // silently delivered somewhere else.
    for (const channel of [256, -1, 1000, 1.5, Number.NaN]) {
      expect(() => encodeFrame(channel, new Uint8Array())).toThrow("channel must be 0..255");
    }
    for (const flags of [256, -1, 1.5, Number.NaN]) {
      expect(() => encodeFrame(CHANNEL_DATA, new Uint8Array(), flags)).toThrow("flags must be 0..255");
    }
  });

  it("takes an empty payload, which is how end-of-file is sent", () => {
    expect([...encodeFrame(CHANNEL_DATA, new Uint8Array(), FLAG_EOF)]).toEqual([1, 1]);
  });

  it("does not copy the payload's bytes wrongly", () => {
    const payload = new TextEncoder().encode("héllo ☃");
    expect(encodeFrame(CHANNEL_DATA, payload).slice(2)).toEqual(payload);
  });
});

describe("decoding a frame", () => {
  it.each(golden.decoded)("$name", (record) => {
    const raw = fromHex(record.raw);
    if (record.error !== undefined) {
      expect(() => decodeFrame(raw)).toThrow(record.error);
      return;
    }
    const frame = decodeFrame(raw);
    expect({
      channel: frame.channel,
      flags: frame.flags,
      payload: toLatin1(frame.payload),
      is_eof: isEof(frame),
      is_control: isControl(frame),
    }).toEqual({
      channel: record.channel,
      flags: record.flags,
      payload: record.payload,
      is_eof: record.is_eof,
      is_control: record.is_control,
    });
  });

  it("refuses anything shorter than a header", () => {
    // Not a frame with an empty payload: a frame is its two header bytes and
    // then whatever follows.
    for (const raw of [new Uint8Array(), Uint8Array.of(1)]) {
      expect(() => decodeFrame(raw)).toThrow("frame too short");
    }
    expect(decodeFrame(Uint8Array.of(1, 0)).payload).toEqual(new Uint8Array());
  });

  it("reads end-of-file from any flags carrying the bit", () => {
    expect(isEof(decodeFrame(Uint8Array.of(1, 0x01)))).toBe(true);
    expect(isEof(decodeFrame(Uint8Array.of(1, 0xff)))).toBe(true);
    expect(isEof(decodeFrame(Uint8Array.of(1, 0x02)))).toBe(false);
    expect(isEof(decodeFrame(Uint8Array.of(1, 0x00)))).toBe(false);
  });

  it("round-trips whatever it encoded", () => {
    const payload = new TextEncoder().encode("héllo ☃");
    const frame = decodeFrame(encodeFrame(CHANNEL_TCP, payload, FLAG_EOF));
    expect(frame).toEqual({ channel: CHANNEL_TCP, flags: FLAG_EOF, payload });
  });
});

describe("control messages", () => {
  it.each(golden.control_encoded)("encoding $name", (record) => {
    if (record.error !== undefined) {
      expect(() => encodeControl(record.message)).toThrow(record.error);
      return;
    }
    expect(toHex(encodeControl(record.message))).toBe(record.value);
  });

  it.each(golden.control_decoded)("decoding $name", (record) => {
    const payload = fromLatin1(record.payload);
    if (record.error !== undefined) {
      expect(() => decodeControl(payload)).toThrow(record.error);
      return;
    }
    expect(decodeControl(payload)).toEqual(record.value);
  });

  it("refuses a message the far end could not dispatch", () => {
    // Without a type there is nowhere to deliver it, so it is refused here
    // rather than sent and dropped.
    expect(() => encodeControl({})).toThrow("control message must have a 'type' key");
    expect(() => encodeControl({ cols: 80 })).toThrow("control message must have a 'type' key");
  });

  it("takes a type that is present but empty, since presence is the rule", () => {
    // The reference asks whether the key is there, not whether it is useful.
    expect(() => encodeControl({ type: null })).not.toThrow();
    expect(() => encodeControl({ type: "" })).not.toThrow();
  });

  it("escapes anything outside ASCII, as the reference's json does", () => {
    // Both ends must produce the same bytes, and Python escapes by default
    // where this runtime would not.
    const encoded = new TextDecoder().decode(encodeControl({ type: "héllo ☃" }).slice(2));
    expect(encoded).toBe('{"type":"h\\u00e9llo \\u2603"}');
  });

  it("writes it compactly, with no spaces to differ over", () => {
    expect(new TextDecoder().decode(encodeControl({ type: "resize", cols: 80 }).slice(2))).toBe(
      '{"type":"resize","cols":80}',
    );
  });

  it("sends it on the control channel and nowhere else", () => {
    expect(encodeControl({ type: "hello" })[0]).toBe(CHANNEL_CONTROL);
  });

  it("refuses JSON that is not an object", () => {
    // A list or a bare string has no type to dispatch on.
    for (const payload of ["[1,2]", '"hello"', "42", "null", "true"]) {
      expect(() => decodeControl(new TextEncoder().encode(payload))).toThrow("control payload must be a JSON object");
    }
  });

  it("refuses what it cannot read at all", () => {
    for (const payload of [new Uint8Array(), new TextEncoder().encode("{"), Uint8Array.of(0xff, 0xfe)]) {
      expect(() => decodeControl(payload)).toThrow("invalid control payload");
    }
  });

  it("round-trips a message through both halves", () => {
    const message = { type: "resize", cols: 80, rows: 25 };
    expect(decodeControl(decodeFrame(encodeControl(message)).payload)).toEqual(message);
  });
});
