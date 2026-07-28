//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Parsing a telnet byte stream.
 *
 * Port of the parser in `provide.uterm.embed.telnet_upstream`.
 *
 * Bytes arrive from a socket in whatever sizes the network chose, so a command
 * can be split across two reads. This separates payload from negotiation and
 * reports how much it consumed; the caller keeps the rest for next time.
 */

/** The byte that introduces a command. */
const IAC = 255;

/** The four option negotiations, each followed by one option byte. */
const WILL = 251;
const WONT = 252;
const DO = 253;
const DONT = 254;

/** Subnegotiation start and end. */
const SB = 250;
const SE = 240;

/** One negotiation the parser lifted out of the stream. */
export interface TelnetEvent {
  /** Whether this is a subnegotiation rather than a simple option. */
  isSub: boolean;
  cmd: number;
  opt: number;
  /** A subnegotiation's body, after its option byte. */
  body: Uint8Array;
}

/** What one pass over a buffer produced. */
export interface TelnetParse {
  payload: Uint8Array;
  events: TelnetEvent[];
  /**
   * How many bytes were taken. The caller keeps the rest — an incomplete
   * command is deliberately left behind.
   */
  consumed: number;
}

/**
 * Double every command byte, so a payload byte of 255 survives the wire.
 *
 * Without this a byte the application meant literally would be read as the
 * start of a negotiation by whatever is upstream.
 */
export function escapeIac(data: Uint8Array): Uint8Array {
  const out: number[] = [];
  for (const byte of data) {
    out.push(byte);
    if (byte === IAC) {
      out.push(IAC);
    }
  }
  return Uint8Array.from(out);
}

/**
 * Split a buffer into payload and negotiations.
 *
 * An incomplete command is not consumed: it stays in the buffer until the
 * bytes that finish it arrive, which is why `consumed` is reported separately
 * — a parser that consumed a half-read command would lose it.
 *
 * Unless `final` is set. Then there will be no more bytes, so a trailing
 * partial command is emitted as payload rather than held forever: half a
 * negotiation is not worth losing the text before it.
 */
export function parseTelnetBuffer(buffer: Uint8Array, final = false): TelnetParse {
  const payload: number[] = [];
  const events: TelnetEvent[] = [];
  let index = 0;
  let consumed = 0;
  const length = buffer.length;

  while (index < length) {
    if (buffer[index] !== IAC) {
      payload.push(buffer[index] as number);
      index += 1;
      consumed = index;
      continue;
    }

    // A command byte with nothing after it: there is no telling yet what it
    // introduces.
    if (index + 1 >= length) {
      if (final) {
        payload.push(IAC);
        index += 1;
        consumed = index;
      }
      break;
    }

    const command = buffer[index + 1] as number;
    if (command === DO || command === DONT || command === WILL || command === WONT) {
      if (index + 2 >= length) {
        if (final) {
          payload.push(...buffer.subarray(index));
          index = length;
          consumed = index;
        }
        break;
      }
      events.push({ isSub: false, cmd: command, opt: buffer[index + 2] as number, body: new Uint8Array(0) });
      index += 3;
      consumed = index;
      continue;
    }

    if (command === SB) {
      // Scan for the terminator, which is itself a two-byte sequence.
      let scan = index + 2;
      let end: number | undefined;
      while (scan < length - 1) {
        if (buffer[scan] === IAC && buffer[scan + 1] === SE) {
          end = scan + 2;
          break;
        }
        scan += 1;
      }
      if (end === undefined) {
        if (final) {
          payload.push(...buffer.subarray(index));
          index = length;
          consumed = index;
        }
        break;
      }
      const body = buffer.subarray(index + 2, end - 2);
      events.push({
        isSub: true,
        cmd: SB,
        // A subnegotiation with no body at all names no option, and zero is
        // what the reference reports.
        opt: body.length > 0 ? (body[0] as number) : 0,
        body: body.length > 1 ? body.subarray(1) : new Uint8Array(0),
      });
      index = end;
      consumed = index;
      continue;
    }

    if (command === IAC) {
      // A doubled command byte is one literal byte: how a payload byte of 255
      // is carried, and what a parser missing it would read as a command.
      payload.push(IAC);
      index += 2;
      consumed = index;
      continue;
    }

    // Any other command carries no operand and no text.
    index += 2;
    consumed = index;
  }

  return { payload: Uint8Array.from(payload), events, consumed };
}
