//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Character-level input filters for BBS/telnet terminal sessions.
 *
 * These async helpers consume and discard protocol-level byte sequences
 * (telnet IAC commands, ANSI escape sequences) from a byte-at-a-time reader.
 * They are intended for interactive BBS sessions where arrow keys, function
 * keys, and telnet negotiation bytes must be silently discarded rather than
 * leaking into command input.
 *
 * Port of the Python module `provide.uterm.filters` and the Go package
 * `filters`.
 *
 * Usage:
 *
 * ```ts
 * const byte = (await reader.read(1))[0];
 * if (byte === IAC) {
 *   await consumeIac(reader);
 *   continue;
 * }
 * if (byte === ESC) {
 *   await consumeEscape(reader);
 *   continue;
 * }
 * ```
 */

// ---------------------------------------------------------------------------
// Telnet IAC constants (RFC 854)
// ---------------------------------------------------------------------------

/** Interpret As Command — the telnet command introducer. */
export const IAC = 255;
/** Sender will enable an option. */
export const WILL = 251;
/** Sender refuses to enable an option. */
export const WONT = 252;
/** Sender asks the peer to enable an option. */
export const DO = 253;
/** Sender asks the peer to disable an option. */
export const DONT = 254;
/** Begin sub-negotiation. */
export const SB = 250;
/** End sub-negotiation. */
export const SE = 240;

/** ANSI escape introducer. */
export const ESC = 0x1b;

/** CSI introducer, `[`, as it appears after ESC. */
const CSI_INTRODUCER = 0x5b;
/** SS3 introducer, `O`, as it appears after ESC. */
const SS3_INTRODUCER = 0x4f;
/** Inclusive lower bound of the CSI final-byte range. */
const CSI_FINAL_MIN = 0x40;
/** Inclusive upper bound of the CSI final-byte range. */
const CSI_FINAL_MAX = 0x7e;

/** Minimal async reader — only `read(n)` is required. */
export interface ByteReader {
  read(n: number): Promise<Uint8Array>;
}

/**
 * Read a single byte, or `undefined` when the reader is exhausted.
 *
 * The reference implementation spells this as `raw = await reader.read(1)`
 * followed by an `if not raw` guard; every consumer below stops rather than
 * blocking when the stream ends mid-sequence.
 */
async function readByte(reader: ByteReader): Promise<number | undefined> {
  const raw = await reader.read(1);
  return raw.length === 0 ? undefined : raw[0];
}

/**
 * Consume and discard a telnet IAC command sequence.
 *
 * Called after the IAC byte (0xFF) has been read. Handles:
 *
 * - Two-byte commands: WILL/WONT/DO/DONT + option byte
 * - Sub-negotiation: SB ... IAC SE
 * - IAC IAC (escaped 0xFF) — silently discarded
 */
export async function consumeIac(reader: ByteReader): Promise<void> {
  const command = await readByte(reader);
  if (command === undefined) {
    return;
  }

  if (command === WILL || command === WONT || command === DO || command === DONT) {
    await reader.read(1); // option byte
    return;
  }
  if (command === SB) {
    for (;;) {
      const byte = await readByte(reader);
      if (byte === undefined) {
        return;
      }
      if (byte === IAC) {
        const next = await readByte(reader);
        // Exhaustion or SE ends the sub-negotiation; any other byte — an
        // escaped IAC IAC included — leaves the scan running.
        if (next === undefined || next === SE) {
          return;
        }
      }
    }
  }
  // IAC IAC or another command — the command byte is all there was to read.
}

/**
 * Consume and discard an ANSI escape sequence.
 *
 * Called after the ESC byte (0x1B) has been read. Handles:
 *
 * - CSI sequences: ESC `[` ... *final-byte* (arrow keys, function keys)
 * - SS3 sequences: ESC `O` *key* (alternate cursor keys)
 * - Two-character sequences: ESC *letter* (Alt+key combos)
 */
export async function consumeEscape(reader: ByteReader): Promise<void> {
  const introducer = await readByte(reader);
  if (introducer === undefined) {
    return;
  }

  if (introducer === CSI_INTRODUCER) {
    for (;;) {
      const byte = await readByte(reader);
      if (byte === undefined) {
        return;
      }
      if (byte >= CSI_FINAL_MIN && byte <= CSI_FINAL_MAX) {
        return; // final byte
      }
    }
  }
  if (introducer === SS3_INTRODUCER) {
    await reader.read(1);
  }
  // Otherwise ESC + a single character — already consumed.
}
