//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Read a PNG the way a decoder would, for tests that must not depend on the
 * exact compressed stream.
 *
 * zlib's deflate output is not identical across platforms even at the same
 * level and the same reported zlib version: a 1x1 white RGBA pixel compresses
 * to an 11-byte IDAT on macOS and a 13-byte IDAT on Linux (both zlib
 * 1.3.1-e00f703, on arm64 and x86_64 alike). A corpus recorded on one platform
 * therefore cannot be compared byte-for-byte against an encoder running on the
 * other, even though both PNGs are correct and show the same picture.
 *
 * So the invariant these helpers support is the one that actually matters —
 * the stream decodes, and it decodes to the expected pixels — while the
 * deterministic parts of the container (signature, IHDR, chunk order) are
 * still compared byte-for-byte by the callers.
 *
 * Only what this project's encoder writes is understood: one IDAT, filter
 * type 0 on every row.
 */

import { inflateSync } from "node:zlib";

/** The signature plus a complete IHDR chunk: the bytes an encoder cannot vary. */
export const PNG_HEADER_LENGTH = 33;

export interface DecodedPng {
  width: number;
  height: number;
  /** Row-major RGBA, filter bytes removed. */
  pixels: Uint8Array;
}

/** Decode `png`, reading its dimensions from IHDR rather than being told them. */
export function decodePng(png: Uint8Array): DecodedPng {
  const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
  const width = view.getUint32(16);
  const height = view.getUint32(20);
  return { width, height, pixels: decodePngPixels(png, width, height) };
}

/** The pixels a PNG holds, given the dimensions its caller already knows. */
export function decodePngPixels(png: Uint8Array, width: number, height: number): Uint8Array {
  const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
  let offset = 8;
  let raw = new Uint8Array(0);
  while (offset < png.length) {
    const length = view.getUint32(offset);
    const type = String.fromCharCode(...png.slice(offset + 4, offset + 8));
    if (type === "IDAT") {
      raw = new Uint8Array(inflateSync(png.slice(offset + 8, offset + 8 + length)));
      break;
    }
    offset += 12 + length;
  }
  const rowLength = width * 4;
  const pixels = new Uint8Array(rowLength * height);
  for (let y = 0; y < height; y += 1) {
    pixels.set(raw.slice(y * (rowLength + 1) + 1, (y + 1) * (rowLength + 1)), y * rowLength);
  }
  return pixels;
}
