//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A graphical console session, and the PNG a screenshot of one becomes.
 *
 * Port of `provide.uterm.server.gui_session`, itself a port of the C#
 * canonical (`Provide.Uterm/Gui/{Session,Png}.cs`) and the Go `gui` package.
 *
 * A {@link GraphicalSession} is an open connection to a remote console: it
 * captures a framebuffer and injects pointer and key events.
 * {@link MemoryGraphicalSession} is the stub behind the `memory` protocol —
 * the RFB client is a unit of its own. {@link encodeRgbaPng} writes a raw
 * RGBA8888 buffer as a PNG with no imaging dependency, because a screenshot
 * travels as base64 in a JSON response and the decoder at the other end is
 * not this one.
 */

import { constants, deflateSync } from "node:zlib";

/**
 * The largest a single side may be.
 *
 * A hostile `ServerInit` announcing a screen of two billion pixels is what
 * this is for: without it the framebuffer is allocated before anybody has
 * looked at what was announced.
 */
export const MAX_DIMENSION = 8192;

/** An RGBA8888 framebuffer, row-major, four bytes to a pixel. */
export class RgbaImage {
  readonly width: number;
  readonly height: number;
  readonly pixels: Uint8Array;

  constructor(width: number, height: number, pixels?: Uint8Array) {
    if (width <= 0 || height <= 0 || width > MAX_DIMENSION || height > MAX_DIMENSION) {
      throw new RangeError(`invalid framebuffer dimensions: ${width}x${height} (max ${MAX_DIMENSION})`);
    }
    const expected = width * height * 4;
    this.width = width;
    this.height = height;
    if (pixels === undefined) {
      this.pixels = new Uint8Array(expected);
      return;
    }
    if (pixels.length !== expected) {
      throw new RangeError(`pixel buffer length ${pixels.length} does not match ${width}x${height} RGBA (${expected})`);
    }
    // Copied, not kept: a caller still holding the buffer could paint on what
    // everybody else is looking at.
    this.pixels = new Uint8Array(pixels);
  }

  /** A copy whose pixels are its own. */
  clone(): RgbaImage {
    return new RgbaImage(this.width, this.height, this.pixels);
  }
}

/** An open connection to a remote graphical console. */
export interface GraphicalSession {
  /** Capture the console as it stands. */
  screenshot(): RgbaImage;
  /** Move the pointer to `(x, y)` and set the button bitmask. */
  injectPointer(x: number, y: number, buttonMask: number): void;
  /** Press (`down`) or release an X11 keysym. */
  injectKey(keySym: number, down: boolean): void;
}

/** The in-memory console behind the `memory` protocol. */
export class MemoryGraphicalSession implements GraphicalSession {
  readonly #framebuffer: RgbaImage;

  constructor(width = 640, height = 480) {
    this.#framebuffer = new RgbaImage(width, height);
  }

  screenshot(): RgbaImage {
    // A copy: a screenshot sharing its buffer with the console would let
    // whoever took it paint on what everybody else is looking at.
    return this.#framebuffer.clone();
  }

  injectPointer(x: number, y: number, buttonMask: number): void {
    const { width, height, pixels } = this.#framebuffer;
    // Load-bearing in the reference rather than here: a negative index writes
    // to the *end* of a Python bytearray and one past the end raises, where a
    // typed array drops both silently. The guard is what makes the two agree,
    // so it is carried over exactly — which is also why a couple of its
    // spellings cannot be told apart from outside on this runtime.
    if ((buttonMask & 1) === 0 || x < 0 || y < 0 || x >= width || y >= height) {
      return;
    }
    const index = (y * width + x) * 4;
    pixels[index] = 255;
    pixels[index + 1] = 255;
    pixels[index + 2] = 255;
    pixels[index + 3] = 255;
  }

  injectKey(_keySym: number, _down: boolean): void {
    // Nothing: a memory console has no keyboard-driven display, and that it
    // stays blank is what stops a test passing for the wrong reason.
  }
}

/** The PNG signature every decoder looks for. */
const PNG_SIGNATURE = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/**
 * Write raw RGBA8888 pixels as a PNG.
 *
 * `pixels` is row-major, four bytes to a pixel; anything past what the size
 * calls for is ignored.
 */
export function encodeRgbaPng(width: number, height: number, pixels: Uint8Array): Uint8Array {
  if (width <= 0 || height <= 0) {
    throw new RangeError("invalid PNG dimensions");
  }
  const expected = width * height * 4;
  if (pixels.length < expected) {
    // Rather than encoding whatever follows in memory.
    throw new RangeError(`pixel buffer too short: need ${expected}, got ${pixels.length}`);
  }

  // Filter type 0 (none) in front of every scanline, then the row itself.
  const rowLength = width * 4;
  const raw = new Uint8Array(height * (rowLength + 1));
  for (let y = 0; y < height; y += 1) {
    raw[y * (rowLength + 1)] = 0;
    raw.set(pixels.subarray(y * rowLength, (y + 1) * rowLength), y * (rowLength + 1) + 1);
  }

  // A zlib stream — header, deflate, adler32 — is exactly the IDAT payload.
  // Z_RLE matches the Python reference and the C# port byte for byte; the
  // default strategy does not, because node's zlib on Linux makes different
  // match choices from CPython's (see gui_session.py for the full note).
  const idat = new Uint8Array(deflateSync(raw, { level: 9, strategy: constants.Z_RLE }));

  const header = new Uint8Array(13);
  const view = new DataView(header.buffer);
  view.setUint32(0, width);
  view.setUint32(4, height);
  header.set([8, 6, 0, 0, 0], 8);

  const chunks = [chunk("IHDR", header), chunk("IDAT", idat), chunk("IEND", new Uint8Array(0))];
  const total = PNG_SIGNATURE.length + chunks.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(total);
  out.set(PNG_SIGNATURE, 0);
  let offset = PNG_SIGNATURE.length;
  for (const part of chunks) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

/** One length-prefixed, CRC-suffixed PNG chunk. */
function chunk(type: string, data: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + data.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, data.length);
  out.set(
    Uint8Array.from(type, (character) => character.charCodeAt(0)),
    4,
  );
  out.set(data, 8);
  view.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
  return out;
}

/** The CRC table, built once, one entry per byte. */
const CRC_TABLE = Uint32Array.from({ length: 256 }, (_unused, byte) => {
  let value = byte;
  for (let bit = 0; bit < 8; bit += 1) {
    value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
  }
  return value >>> 0;
});

/** The CRC-32 a PNG chunk carries, over its type and its data. */
function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc = (CRC_TABLE[(crc ^ byte) & 0xff] as number) ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
