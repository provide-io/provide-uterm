//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { decodePngPixels } from "../testing/png.ts";
import { encodeRgbaPng, MAX_DIMENSION, MemoryGraphicalSession, RgbaImage } from "./index.ts";

interface GuiGolden {
  max_dimension: number;
  default_size: [number, number];
  screenshot_is_a_copy: boolean;
  images: Array<{
    name: string;
    width: number;
    height: number;
    pixels: number[] | null;
    value?: { width: number; height: number; pixels: string };
    error?: string;
  }>;
  pngs: Array<{
    name: string;
    width: number;
    height: number;
    value?: { length: number; sha256: string; png: string };
    error?: string;
  }>;
  pointers: Array<{
    name: string;
    events: Array<[number, number, number]>;
    width: number;
    height: number;
    pixels: string;
    lit: number[];
  }>;
  default_png: { length: number; sha256: string };
}

const golden = loadGolden<GuiGolden>("guisession_golden.json");

/** The corpus's base64 as the bytes it stands for. */
/** A digest, for comparing large pixel buffers without a deep-equality walk. */
function digestOf(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function bytesOf(encoded: string): Uint8Array {
  return new Uint8Array(Buffer.from(encoded, "base64"));
}

/** What a call did: its value, or the message it refused with. */
function outcomeOf<T>(call: () => T): { value?: T; error?: string } {
  try {
    return { value: call() };
  } catch (error) {
    return { error: (error as Error).message };
  }
}

describe("the framebuffer a console is read into", () => {
  it("caps a side where the reference caps one", () => {
    // A hostile ServerInit announcing a screen of two billion pixels is what
    // this is for.
    expect(MAX_DIMENSION).toBe(golden.max_dimension);
  });

  it.each(golden.images)("$name", (record) => {
    const pixels = record.pixels === null ? undefined : new Uint8Array(record.pixels);
    const actual = outcomeOf(() => new RgbaImage(record.width, record.height, pixels));
    if (record.error !== undefined) {
      expect(actual.error).toBe(record.error);
      return;
    }
    expect(actual.error).toBeUndefined();
    const image = actual.value as RgbaImage;
    expect(image.width).toBe(record.value?.width);
    expect(image.height).toBe(record.value?.height);
    expect(image.pixels).toEqual(bytesOf(record.value?.pixels as string));
  });

  it("starts every pixel at nothing", () => {
    const image = new RgbaImage(2, 2);
    expect(image.pixels.length).toBe(16);
    expect([...image.pixels].every((value) => value === 0)).toBe(true);
  });

  it("keeps nothing of the buffer it was handed", () => {
    // A caller that could still write to the framebuffer could paint on what
    // everybody else is looking at.
    const pixels = new Uint8Array([1, 2, 3, 4]);
    const image = new RgbaImage(1, 1, pixels);
    pixels[0] = 200;
    expect(image.pixels[0]).toBe(1);
  });

  it("copies its pixels when it copies itself", () => {
    const image = new RgbaImage(1, 1, new Uint8Array([1, 2, 3, 4]));
    const copy = image.clone();
    copy.pixels[0] = 200;
    expect(image.pixels[0]).toBe(1);
    expect(copy.width).toBe(1);
    expect(copy.height).toBe(1);
  });

  it("refuses a side that is not a side", () => {
    for (const [width, height] of [
      [0, 1],
      [1, 0],
      [-1, 1],
      [1, -1],
      [MAX_DIMENSION + 1, 1],
      [1, MAX_DIMENSION + 1],
    ] as const) {
      expect(() => new RgbaImage(width, height)).toThrow("invalid framebuffer dimensions");
    }
    expect(new RgbaImage(MAX_DIMENSION, 1).width).toBe(MAX_DIMENSION);
    expect(new RgbaImage(1, MAX_DIMENSION).height).toBe(MAX_DIMENSION);
  });

  it("refuses a buffer that is not the size it claims", () => {
    // Either way round: a short one would be read past its end, a long one
    // means the sender and the reader disagree about the screen.
    expect(() => new RgbaImage(2, 1, new Uint8Array(4))).toThrow("pixel buffer length 4 does not match 2x1 RGBA (8)");
    expect(() => new RgbaImage(2, 1, new Uint8Array(9))).toThrow("pixel buffer length 9 does not match 2x1 RGBA (8)");
  });
});

describe("the in-memory console", () => {
  it("is the size the reference makes one", () => {
    const session = new MemoryGraphicalSession();
    const shot = session.screenshot();
    expect([shot.width, shot.height]).toEqual(golden.default_size);
  });

  it.each(golden.pointers)("$name", (record) => {
    const session = new MemoryGraphicalSession(4, 3);
    for (const [x, y, mask] of record.events) {
      session.injectPointer(x, y, mask);
    }
    session.injectKey(0xff0d, true);
    session.injectKey(0xff0d, false);
    const shot = session.screenshot();
    expect(shot.width).toBe(record.width);
    expect(shot.height).toBe(record.height);
    expect(shot.pixels).toEqual(bytesOf(record.pixels));
  });

  it("hands back a copy, not the console", () => {
    // A screenshot that shared its buffer would let whoever took it paint on
    // what everybody else is looking at.
    expect(golden.screenshot_is_a_copy).toBe(true);
    const session = new MemoryGraphicalSession(2, 2);
    const shot = session.screenshot();
    shot.pixels[0] = 200;
    expect(session.screenshot().pixels[0]).toBe(0);
  });

  it("draws only where the first button is held", () => {
    const session = new MemoryGraphicalSession(2, 2);
    session.injectPointer(0, 0, 2);
    session.injectPointer(1, 0, 4);
    expect([...session.screenshot().pixels].every((value) => value === 0)).toBe(true);
    session.injectPointer(1, 1, 3);
    expect(session.screenshot().pixels[12]).toBe(255);
  });

  it("draws a whole pixel, not part of one", () => {
    // A pixel written three bytes deep is a pixel nobody can see.
    const session = new MemoryGraphicalSession(1, 1);
    session.injectPointer(0, 0, 1);
    expect([...session.screenshot().pixels]).toEqual([255, 255, 255, 255]);
  });

  it("ignores a point that is not on the console", () => {
    const session = new MemoryGraphicalSession(2, 2);
    for (const [x, y] of [
      [-1, 0],
      [0, -1],
      [2, 0],
      [0, 2],
      [99, 99],
    ] as const) {
      session.injectPointer(x, y, 1);
    }
    expect([...session.screenshot().pixels].every((value) => value === 0)).toBe(true);
  });

  it("takes a key and does nothing with it", () => {
    // The stub has no keyboard-driven display; that it stays blank is what
    // stops a test passing for the wrong reason.
    const session = new MemoryGraphicalSession(1, 1);
    session.injectKey(0xff0d, true);
    session.injectKey(0x41, false);
    expect([...session.screenshot().pixels].every((value) => value === 0)).toBe(true);
  });
});

describe("the PNG a screenshot becomes", () => {
  it.each(golden.pngs.filter((record) => record.error !== undefined))("$name", (record) => {
    const actual = outcomeOf(() => encodeRgbaPng(record.width, record.height, new Uint8Array(4)));
    expect(actual.error).toBe(record.error);
  });

  it.each(golden.pngs.filter((record) => record.error === undefined))("$name, byte for byte", (record) => {
    // The client that opens this is not this one, so a stream differing by a
    // single chunk is a screenshot that does not open. This is byte-for-byte
    // across languages and platforms because every port compresses with level 9
    // and the run-length strategy -- see the note in ../gui/session.ts.
    const source = bytesOf(record.value?.png as string);
    const pixels = decodePngPixels(source, record.width, record.height);
    const encoded = encodeRgbaPng(record.width, record.height, pixels);
    expect(encoded.length).toBe(record.value?.length);
    expect(digestOf(encoded)).toBe(record.value?.sha256);
  });

  it("writes the same stream for a screen nobody would paste into a test", () => {
    const session = new MemoryGraphicalSession();
    const shot = session.screenshot();
    const encoded = encodeRgbaPng(shot.width, shot.height, shot.pixels);
    expect(encoded.length).toBe(golden.default_png.length);
    expect(digestOf(encoded)).toBe(golden.default_png.sha256);
  });

  it("starts with the signature every decoder looks for", () => {
    const encoded = encodeRgbaPng(1, 1, new Uint8Array([0, 0, 0, 255]));
    expect([...encoded.slice(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  });

  it("declares the size and the colour the pixels actually are", () => {
    // Eight bits a channel, colour type 6 (RGBA), no interlacing: a header
    // that lies about any of them decodes to noise.
    const encoded = encodeRgbaPng(3, 2, new Uint8Array(24));
    const view = new DataView(encoded.buffer, encoded.byteOffset, encoded.byteLength);
    expect(String.fromCharCode(...encoded.slice(12, 16))).toBe("IHDR");
    expect(view.getUint32(16)).toBe(3);
    expect(view.getUint32(20)).toBe(2);
    expect([...encoded.slice(24, 29)]).toEqual([8, 6, 0, 0, 0]);
  });

  it("ends where a decoder stops reading", () => {
    const encoded = encodeRgbaPng(1, 1, new Uint8Array(4));
    expect(String.fromCharCode(...encoded.slice(-8, -4))).toBe("IEND");
    expect([...encoded.slice(-12, -8)]).toEqual([0, 0, 0, 0]);
  });

  it("ignores whatever follows the pixels it was promised", () => {
    const exact = encodeRgbaPng(1, 1, new Uint8Array([1, 2, 3, 4]));
    const extra = encodeRgbaPng(1, 1, new Uint8Array([1, 2, 3, 4, 9, 9, 9, 9]));
    expect(extra).toEqual(exact);
  });

  it("refuses a buffer with fewer pixels than the size it was given", () => {
    // Rather than encoding whatever follows in memory.
    expect(() => encodeRgbaPng(2, 2, new Uint8Array(4))).toThrow("pixel buffer too short: need 16, got 4");
  });

  it("refuses a size that is not a size", () => {
    for (const [width, height] of [
      [0, 1],
      [1, 0],
      [-1, 1],
      [1, -1],
    ] as const) {
      expect(() => encodeRgbaPng(width, height, new Uint8Array(0))).toThrow("invalid PNG dimensions");
    }
  });
});

