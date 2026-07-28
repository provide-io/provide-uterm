//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type ByteReader, DO, DONT, ESC, IAC, SB, SE, WILL, WONT, consumeEscape, consumeIac } from "./index.ts";

interface FiltersGolden {
  constants: Record<string, number>;
  iac: Array<{ name: string; stream: string; consumed: number; remaining: string }>;
  escape: Array<{ name: string; stream: string; consumed: number; remaining: string }>;
}

const golden = loadGolden<FiltersGolden>("filters_golden.json");

/** Byte-at-a-time reader over a fixed buffer, tracking how far it was read. */
class BufferReader implements ByteReader {
  position = 0;
  readonly data: Uint8Array;

  constructor(data: Uint8Array) {
    this.data = data;
  }

  read(n: number): Promise<Uint8Array> {
    const chunk = this.data.subarray(this.position, this.position + n);
    this.position += chunk.length;
    return Promise.resolve(chunk);
  }
}

/** Decode a lowercase hex string into bytes, matching Python's `bytes.hex()`. */
function fromHex(hex: string): Uint8Array {
  return new Uint8Array(Buffer.from(hex, "hex"));
}

/** Run a consumer over `stream` and report how many bytes it read. */
async function consumed(consumer: (reader: ByteReader) => Promise<void>, stream: Uint8Array): Promise<number> {
  const reader = new BufferReader(stream);
  await consumer(reader);
  return reader.position;
}

describe("telnet constants", () => {
  it("matches the RFC 854 values used by the reference implementation", () => {
    expect({ IAC, WILL, WONT, DO, DONT, SB, SE, ESC }).toStrictEqual({
      IAC: 255,
      WILL: 251,
      WONT: 252,
      DO: 253,
      DONT: 254,
      SB: 250,
      SE: 240,
      ESC: 0x1b,
    });
  });
});

describe("consumeIac", () => {
  it("reads nothing when the stream is already exhausted", async () => {
    expect(await consumed(consumeIac, new Uint8Array())).toBe(0);
  });

  it.each([
    ["WILL", WILL],
    ["WONT", WONT],
    ["DO", DO],
    ["DONT", DONT],
  ])("consumes the option byte after %s", async (_name, command) => {
    expect(await consumed(consumeIac, new Uint8Array([command, 1, 0x72]))).toBe(2);
  });

  it("stops when the option byte is missing", async () => {
    expect(await consumed(consumeIac, new Uint8Array([WILL]))).toBe(1);
  });

  it("consumes only the second byte of an escaped IAC IAC", async () => {
    expect(await consumed(consumeIac, new Uint8Array([IAC, 0x72]))).toBe(1);
  });

  it("consumes only the command byte of an unknown command", async () => {
    expect(await consumed(consumeIac, new Uint8Array([0x01, 0x72]))).toBe(1);
  });

  it("consumes a subnegotiation up to and including IAC SE", async () => {
    expect(await consumed(consumeIac, new Uint8Array([SB, 0x18, 0x00, IAC, SE, 0x72]))).toBe(5);
  });

  it("keeps scanning past an escaped IAC inside a subnegotiation", async () => {
    expect(await consumed(consumeIac, new Uint8Array([SB, 0x18, IAC, IAC, 0x00, IAC, SE, 0x72]))).toBe(7);
  });

  it("keeps scanning when IAC is followed by a byte that is not SE", async () => {
    expect(await consumed(consumeIac, new Uint8Array([SB, 0x18, IAC, 0x00, IAC, SE, 0x72]))).toBe(6);
  });

  it("stops at the end of a truncated subnegotiation", async () => {
    expect(await consumed(consumeIac, new Uint8Array([SB, 0x18, 0x00]))).toBe(3);
    expect(await consumed(consumeIac, new Uint8Array([SB, 0x18, IAC]))).toBe(3);
  });
});

describe("consumeEscape", () => {
  it("reads nothing when the stream is already exhausted", async () => {
    expect(await consumed(consumeEscape, new Uint8Array())).toBe(0);
  });

  it("consumes a CSI sequence up to its final byte", async () => {
    expect(await consumed(consumeEscape, new Uint8Array([0x5b, 0x41, 0x72]))).toBe(2);
  });

  it("consumes CSI parameter bytes before the final byte", async () => {
    expect(await consumed(consumeEscape, new Uint8Array([0x5b, 0x31, 0x3b, 0x32, 0x48, 0x72]))).toBe(5);
  });

  it.each([
    ["the low end", 0x40],
    ["the high end", 0x7e],
  ])("treats a byte at %s of the final-byte range as terminal", async (_name, final) => {
    expect(await consumed(consumeEscape, new Uint8Array([0x5b, final, 0x72]))).toBe(2);
  });

  it("does not treat a byte just below the final-byte range as terminal", async () => {
    // 0x3f is '?', a private-mode introducer, not a final byte.
    expect(await consumed(consumeEscape, new Uint8Array([0x5b, 0x3f, 0x31, 0x68, 0x72]))).toBe(4);
  });

  it("stops at the end of a truncated CSI sequence", async () => {
    expect(await consumed(consumeEscape, new Uint8Array([0x5b, 0x31]))).toBe(2);
    expect(await consumed(consumeEscape, new Uint8Array([0x5b]))).toBe(1);
  });

  it("consumes the key byte of an SS3 sequence", async () => {
    expect(await consumed(consumeEscape, new Uint8Array([0x4f, 0x50, 0x72]))).toBe(2);
  });

  it("stops when the SS3 key byte is missing", async () => {
    expect(await consumed(consumeEscape, new Uint8Array([0x4f]))).toBe(1);
  });

  it("consumes only the second character of a two-character combo", async () => {
    expect(await consumed(consumeEscape, new Uint8Array([0x61, 0x72]))).toBe(1);
    expect(await consumed(consumeEscape, new Uint8Array([ESC, 0x72]))).toBe(1);
  });
});

describe("differential parity with CPython", () => {
  it("exposes the same constant values the reference recorded", () => {
    expect({ IAC, WILL, WONT, DO, DONT, SB, SE, ESC }).toStrictEqual(golden.constants);
  });

  it("consumes exactly what CPython consumed for every IAC record", async () => {
    for (const record of golden.iac) {
      const stream = fromHex(record.stream);
      const reader = new BufferReader(stream);
      await consumeIac(reader);
      expect({
        name: record.name,
        consumed: reader.position,
        remaining: Buffer.from(stream.subarray(reader.position)).toString("hex"),
      }).toStrictEqual({ name: record.name, consumed: record.consumed, remaining: record.remaining });
    }
    expect(golden.iac.length).toBeGreaterThan(10);
  });

  it("consumes exactly what CPython consumed for every escape record", async () => {
    for (const record of golden.escape) {
      const stream = fromHex(record.stream);
      const reader = new BufferReader(stream);
      await consumeEscape(reader);
      expect({
        name: record.name,
        consumed: reader.position,
        remaining: Buffer.from(stream.subarray(reader.position)).toString("hex"),
      }).toStrictEqual({ name: record.name, consumed: record.consumed, remaining: record.remaining });
    }
    expect(golden.escape.length).toBeGreaterThan(10);
  });
});
