//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { channelStrToBytes, wsFrameToChannelStr } from "./index.ts";

/** Render bytes as lowercase hex, matching Python's `bytes.hex()`. */
function hex(data: Uint8Array): string {
  return Buffer.from(data).toString("hex");
}

describe("wsFrameToChannelStr", () => {
  it("passes a text frame through unchanged", () => {
    expect(wsFrameToChannelStr("hello")).toBe("hello");
  });

  it("decodes a binary frame as latin-1 so every byte survives", () => {
    expect(wsFrameToChannelStr(new Uint8Array([0x00, 0x41, 0x80, 0x9f, 0xff]))).toBe("\x00\x41\x80\x9f\xff");
  });

  it("maps all 256 byte values one-to-one onto code points", () => {
    const all = new Uint8Array(256);
    for (let i = 0; i < 256; i += 1) {
      all[i] = i;
    }
    const decoded = wsFrameToChannelStr(all);
    expect(decoded.length).toBe(256);
    for (let i = 0; i < 256; i += 1) {
      expect(decoded.codePointAt(i)).toBe(i);
    }
  });

  it("preserves the C1 range that a CP437 round-trip would destroy", () => {
    // cp437 has no code point for U+0080-U+009F, so a latin-1 to cp437
    // round-trip would replace every byte in that range with '?'.
    const c1 = new Uint8Array(32);
    for (let i = 0; i < 32; i += 1) {
      c1[i] = 0x80 + i;
    }
    expect([...wsFrameToChannelStr(c1)].map((c) => c.codePointAt(0))).toStrictEqual([...c1]);
  });
});

describe("channelStrToBytes", () => {
  it("recovers the original bytes from a channel string", () => {
    expect(hex(channelStrToBytes("\x00\x41\x80\x9f\xff"))).toBe("0041809fff");
  });

  it("round-trips every byte value through the shim", () => {
    const all = new Uint8Array(256);
    for (let i = 0; i < 256; i += 1) {
      all[i] = i;
    }
    expect(hex(channelStrToBytes(wsFrameToChannelStr(all)))).toBe(hex(all));
  });

  it("replaces a code point above the latin-1 range rather than throwing", () => {
    // CPython uses errors="replace", which emits '?' (0x3f).
    expect(hex(channelStrToBytes("aĀb"))).toBe("613f62");
    expect(hex(channelStrToBytes("你"))).toBe("3f");
  });

  it("replaces an astral code point with a single byte", () => {
    expect(hex(channelStrToBytes("𝄞"))).toBe("3f");
  });
});
