//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Read back a PNG this project wrote, so the encoder can be fed what produced
 * a corpus entry.
 *
 * Only what that encoder writes is understood: one IDAT, filter type 0 on
 * every row.
 */

import { inflateSync } from "node:zlib";

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
