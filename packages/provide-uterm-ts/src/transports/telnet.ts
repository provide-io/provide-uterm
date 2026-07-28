//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * RFC 854 telnet framing.
 *
 * Port of the Python modules `provide.uterm.transports._telnet_const` and
 * `...telnet_transport`.
 *
 * Telnet signals in band: the command byte is `0xFF`, which is also a byte a
 * terminal legitimately sends. Every layer therefore has to agree on where a
 * command starts and ends, and disagreeing does not fail loudly — it puts
 * stray command bytes on the operator's screen, or swallows screen content as
 * though it were a negotiation.
 */

/** Telnet command and option bytes. */
export const TELNET = {
  /** Interpret As Command. */
  IAC: 255,
  /** Sender will perform an option. */
  WILL: 251,
  /** Sender will not perform an option. */
  WONT: 252,
  /** Receiver should perform an option. */
  DO: 253,
  /** Receiver should not perform an option. */
  DONT: 254,
  /** Subnegotiation begins. */
  SB: 250,
  /** Subnegotiation ends. */
  SE: 240,
  /** Binary transmission. */
  OPT_BINARY: 0,
  /** Echo. */
  OPT_ECHO: 1,
  /** Suppress go-ahead. */
  OPT_SGA: 3,
  /** Negotiate about window size. */
  OPT_NAWS: 31,
  /** Terminal type. */
  OPT_TTYPE: 24,
} as const;

/** Options the client agrees to perform when asked. */
const DO_ACCEPTED = new Set<number>([TELNET.OPT_BINARY, TELNET.OPT_SGA, TELNET.OPT_NAWS, TELNET.OPT_TTYPE]);

/** Options the client is happy for the far end to perform. */
const WILL_ACCEPTED = new Set<number>([TELNET.OPT_ECHO, TELNET.OPT_SGA, TELNET.OPT_BINARY]);

/** A negotiation the far end sent. */
export interface NegotiateEvent {
  kind: "negotiate";
  /** One of WILL, WONT, DO, DONT. */
  command: number;
  /** Which option it concerns. */
  option: number;
}

/** A subnegotiation block the far end sent. */
export interface SubnegotiationEvent {
  kind: "subnegotiation";
  /** The block's contents, without its framing. */
  payload: Uint8Array;
}

/** What the parser found. */
export interface TelnetParseResult {
  /** Application bytes, with commands removed and escapes undone. */
  payload: Uint8Array;
  /** Commands the far end sent. */
  events: Array<NegotiateEvent | SubnegotiationEvent>;
  /**
   * How many input bytes were fully handled.
   *
   * The rest is an incomplete sequence the caller should keep and re-present
   * with the next read.
   */
  consumed: number;
}

/** How to answer a DO. */
export interface DoReply {
  /** WILL to accept, WONT to refuse. */
  command: number;
  /** What to send immediately after accepting, when the option needs it. */
  thenSend?: "naws" | "ttype";
}

/**
 * Index just past the SE ending a subnegotiation, or nothing if incomplete.
 *
 * The bound stops one short so the pair read below is always in range;
 * running to the end instead is harmless — the out-of-range read is
 * `undefined`, which is never SE — but the shorter bound says the intent.
 */
function findSubnegotiationEnd(data: Uint8Array, start: number): number | undefined {
  for (let index = start; index < data.length - 1; index += 1) {
    if (data[index] === TELNET.IAC && data[index + 1] === TELNET.SE) {
      return index + 2;
    }
  }
  return undefined;
}

/**
 * Split telnet commands out of a buffer.
 *
 * While `final` is false an incomplete sequence at the end is left
 * unconsumed — the next read may complete it, and treating a trailing
 * command byte as data is the classic split-read bug. Once nothing more is
 * coming, `final` makes the same input emit those bytes literally instead,
 * so they are not silently dropped.
 */
export function parseTelnetBuffer(data: Uint8Array, final = false): TelnetParseResult {
  const payload: number[] = [];
  const events: Array<NegotiateEvent | SubnegotiationEvent> = [];
  let index = 0;
  let consumed = 0;

  while (index < data.length) {
    if (data[index] !== TELNET.IAC) {
      payload.push(data[index] as number);
      index += 1;
      consumed = index;
      continue;
    }

    if (index + 1 >= data.length) {
      if (final) {
        payload.push(TELNET.IAC);
        index += 1;
        consumed = index;
      }
      break;
    }

    const command = data[index + 1] as number;
    if (command === TELNET.DO || command === TELNET.DONT || command === TELNET.WILL || command === TELNET.WONT) {
      if (index + 2 >= data.length) {
        if (final) {
          payload.push(...data.subarray(index));
          index = data.length;
          consumed = index;
        }
        break;
      }
      events.push({ kind: "negotiate", command, option: data[index + 2] as number });
      index += 3;
      consumed = index;
      continue;
    }

    if (command === TELNET.SB) {
      const end = findSubnegotiationEnd(data, index + 2);
      if (end === undefined) {
        // A subnegotiation payload has no length bound, so there is no point
        // at which to give up on one mid-stream.
        if (final) {
          payload.push(...data.subarray(index));
          index = data.length;
          consumed = index;
        }
        break;
      }
      events.push({ kind: "subnegotiation", payload: data.slice(index + 2, end - 2) });
      index = end;
      consumed = index;
      continue;
    }

    if (command === TELNET.IAC) {
      // A doubled command byte is one literal byte of data.
      payload.push(TELNET.IAC);
      index += 2;
      consumed = index;
      continue;
    }

    // An unrecognised command is still a command: dropping it is right, and
    // rendering it would corrupt the screen.
    index += 2;
    consumed = index;
  }

  return { payload: Uint8Array.from(payload), events, consumed };
}

/**
 * Double any command byte so it survives the wire as data.
 *
 * Without this a `0xFF` the user typed would be read as the start of a
 * command by the far end.
 */
export function escapeTelnetData(data: Uint8Array): Uint8Array {
  const out: number[] = [];
  for (const byte of data) {
    out.push(byte);
    if (byte === TELNET.IAC) {
      out.push(TELNET.IAC);
    }
  }
  return Uint8Array.from(out);
}

/**
 * How to answer a DO for `option`.
 *
 * Refusing window size or terminal type would leave a BBS drawing for the
 * wrong geometry, so both are accepted — and each is followed immediately by
 * the value itself, since agreeing and then never saying leaves the server
 * guessing.
 */
export function replyToDo(option: number): DoReply {
  if (!DO_ACCEPTED.has(option)) {
    return { command: TELNET.WONT };
  }
  if (option === TELNET.OPT_NAWS) {
    return { command: TELNET.WILL, thenSend: "naws" };
  }
  if (option === TELNET.OPT_TTYPE) {
    return { command: TELNET.WILL, thenSend: "ttype" };
  }
  return { command: TELNET.WILL };
}

/** How to answer a WILL for `option`. */
export function replyToWill(option: number): number {
  return WILL_ACCEPTED.has(option) ? TELNET.DO : TELNET.DONT;
}

/**
 * Accumulates reads and parses whole sequences out of them.
 *
 * A socket splits wherever it likes, including mid-command, which is exactly
 * why the parser reports how much it consumed.
 */
export class TelnetBuffer {
  #held: number[] = [];

  /** How many bytes are being held for the next read. */
  get pending(): number {
    return this.#held.length;
  }

  /** Add a read and take whatever is now complete. */
  feed(data: Uint8Array): TelnetParseResult {
    this.#held.push(...data);
    const result = parseTelnetBuffer(Uint8Array.from(this.#held));
    this.#held = this.#held.slice(result.consumed);
    return result;
  }

  /** Take everything left, treating incomplete sequences as data. */
  flush(): TelnetParseResult {
    const result = parseTelnetBuffer(Uint8Array.from(this.#held), true);
    this.#held = [];
    return result;
  }
}
