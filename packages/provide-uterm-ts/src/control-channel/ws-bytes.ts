//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Lossless byte ↔ string shim for the inline DLE/STX control-frame stream.
 *
 * The control-frame API is a string-typed protocol: `ControlFrameDecoder`
 * takes strings and `DataChunk.data` is a string. But the data carried
 * inside it is raw terminal bytes — typically CP437 from a BBS — and must
 * not lose any high bytes between the WebSocket boundary and the terminal
 * emulator.
 *
 * Latin-1 is the shim because it maps bytes 0x00-0xFF to code points
 * U+0000-U+00FF one-to-one with no replacements. CP437 is *not* a valid shim
 * here: cp437 has no code point for U+0080-U+009F, so a latin-1 → cp437
 * round-trip silently replaces every byte in that range with `?` and
 * destroys box-drawing characters.
 *
 * CP437 decoding happens *exactly once*, inside the terminal emulator.
 * Everything upstream stays byte-faithful.
 *
 * Port of the Python module `provide.uterm.ws_bytes`.
 */

/**
 * Coerce a WebSocket frame into the string form the decoder expects.
 *
 * Binary frames are decoded as latin-1 so every byte survives as a code
 * point. Text frames pass through — the sender is responsible for not
 * emitting non-latin-1 code points into the channel.
 */
export function wsFrameToChannelStr(raw: string | Uint8Array): string {
  if (typeof raw === "string") {
    return raw;
  }
  return Buffer.from(raw).toString("latin1");
}

/**
 * Recover raw terminal bytes from a `DataChunk.data` string.
 *
 * Inverse of {@link wsFrameToChannelStr} for the data segment. The result is
 * the original byte stream to feed to a terminal emulator, which performs
 * its own CP437 decode internally.
 *
 * Code points above U+00FF cannot be represented and are replaced with `?`,
 * matching CPython's `encode("latin-1", errors="replace")`.
 */
export function channelStrToBytes(data: string): Uint8Array {
  const out = new Uint8Array(data.length);
  let length = 0;
  for (const char of data) {
    const codePoint = char.codePointAt(0) as number;
    out[length] = codePoint > 0xff ? 0x3f : codePoint;
    length += 1;
  }
  return out.subarray(0, length);
}
