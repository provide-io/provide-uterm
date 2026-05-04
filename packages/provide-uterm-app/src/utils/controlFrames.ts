//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Decode inline control frames from the terminal WebSocket stream.
 *
 * The wire format uses DLE (0x10) STX (0x02) framing:
 *   DLE STX [8-hex-digit-length] : [json-payload]
 */

const DLE = "\x10";
const STX = "\x02";

export function decodeControlFrames(raw: string): Array<Record<string, unknown>> {
  const frames: Array<Record<string, unknown>> = [];
  let pos = 0;
  while (pos < raw.length) {
    const dleIdx = raw.indexOf(DLE, pos);
    if (dleIdx === -1) break;
    if (dleIdx + 1 < raw.length && raw[dleIdx + 1] === STX) {
      const header = raw.substring(dleIdx + 2, dleIdx + 10);
      if (header.length === 8 && raw[dleIdx + 10] === ":") {
        const len = parseInt(header, 16);
        const json = raw.substring(dleIdx + 11, dleIdx + 11 + len);
        try {
          frames.push(JSON.parse(json) as Record<string, unknown>);
        } catch {
          /* skip malformed */
        }
        pos = dleIdx + 11 + len;
        continue;
      }
    }
    pos = dleIdx + 1;
  }
  return frames;
}
