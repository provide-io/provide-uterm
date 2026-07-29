//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Working out what kind of terminal is at the far end of a telnet connection.
 *
 * Port of `provide.uterm.gateway._iac_negotiate`. Before anything useful
 * crosses, the gateway asks the client two questions — what terminal it is,
 * and what its environment says — and the answers decide the colour mode the
 * upstream session is opened with.
 *
 * * **Every IAC byte comes out of the stream.** What the client typed is what
 *   goes upstream; a stray `IAC` reaching a shell is a byte nobody typed.
 * * **A subnegotiation can arrive in pieces**, since this is a byte stream, so
 *   what is incomplete is held and carried on with.
 * * **The buffer is capped**, so a client that opens a subnegotiation and
 *   never closes it costs a fixed amount of memory rather than all of it.
 */

/** The telnet vocabulary this needs (RFC 854 and the option codes). */
const IAC = 255;
const DONT = 254;
const DO = 253;
const WONT = 252;
const WILL = 251;
const SB = 250;
const SE = 240;

/** Terminal type (RFC 1091). */
const OPT_TTYPE = 24;
/** Environment (RFC 1572). */
const OPT_NEW_ENVIRON = 39;

const SUB_IS = 0;
const SUB_SEND = 1;
const ENV_VAR = 0;
const ENV_VALUE = 1;
const ENV_ESC = 2;
const ENV_USERVAR = 3;

/** Terminals whose colour support is known without asking. */
const LEGACY_TERMINALS = new Set(["xterm", "vt100", "vt102", "vt220", "ansi", "linux", "dumb"]);

/** How much of an unfinished subnegotiation is kept. */
export const MAX_SUBNEGOTIATION_BYTES = 512;

/** What a client says back, and what goes on upstream. */
export interface IacFeedResult {
  /** Bytes to write back to the client. */
  reply: Uint8Array;
  /** The client's own data, with every IAC sequence taken out. */
  cleaned: Uint8Array;
}

/** `IAC SB <option> SEND IAC SE` — asking a client to send something. */
function askFor(option: number): Uint8Array {
  return Uint8Array.from([IAC, SB, option, SUB_SEND, IAC, SE]);
}

/**
 * The colour mode a terminal and environment imply.
 *
 * A true-colour hint wins outright; otherwise a terminal naming 256 colours
 * gets 256, and one this recognises at all gets 16. A terminal nobody
 * recognises gets nothing, so the upstream is left to decide rather than
 * being told something wrong.
 */
export function deriveColormode(
  term: string | undefined,
  env: Readonly<Record<string, string>> = {},
): string | undefined {
  const colorterm = (env.COLORTERM ?? "").trim().toLowerCase();
  if (colorterm === "truecolor" || colorterm === "24bit") {
    return "passthrough";
  }
  // A terminal named by the client wins; failing that, one it put in its
  // environment.
  const name = (term === undefined || term === "" ? (env.TERM ?? "") : term).trim().toLowerCase();
  if (name.endsWith("-direct") || name.endsWith("-truecolor")) {
    return "passthrough";
  }
  if (name.endsWith("-256color")) {
    return "256";
  }
  // Named exactly, not by prefix: `xterm-color` is not one of these, and
  // guessing sixteen colours for a terminal nobody listed would be telling
  // the upstream something that might be wrong.
  if (LEGACY_TERMINALS.has(name)) {
    return "16";
  }
  return undefined;
}

/** Follows the handshake far enough to learn what the client is. */
export class IacNegotiator {
  /** What the client said it is. */
  term = "";
  /** What the client said about its environment. */
  readonly env: Record<string, string> = {};

  #pending: number[] = [];
  #inSubnegotiation = false;
  #subnegotiation: number[] = [];
  #ttypeRequested = false;
  #newEnvironRequested = false;
  #ttypeReceived = false;
  #newEnvironReceived = false;

  /** What to send the moment a client connects. */
  startBytes(): Uint8Array {
    this.#ttypeRequested = true;
    this.#newEnvironRequested = true;
    return Uint8Array.from([IAC, DO, OPT_TTYPE, IAC, DO, OPT_NEW_ENVIRON]);
  }

  /** Whether every question asked has been answered. */
  done(): boolean {
    const ttypeOk = !this.#ttypeRequested || this.#ttypeReceived;
    const environOk = !this.#newEnvironRequested || this.#newEnvironReceived;
    return ttypeOk && environOk;
  }

  /** The colour mode implied by what the client has said so far. */
  derivedColormode(): string | undefined {
    return deriveColormode(this.term, this.env);
  }

  /**
   * Consume bytes from the client.
   *
   * Anything left half-finished is held for the next call, so a sequence split
   * across two reads is completed rather than lost.
   */
  feed(data: Uint8Array): IacFeedResult {
    const reply: number[] = [];
    const cleaned: number[] = [];
    // Whatever was held back last time goes in front of what has just come.
    const bytes = this.#pending.length === 0 ? [...data] : [...this.#pending, ...data];
    this.#pending = [];

    let index = 0;
    while (index < bytes.length) {
      const byte = bytes[index] as number;

      if (this.#inSubnegotiation) {
        index = this.#consumeSubnegotiation(bytes, index);
        continue;
      }

      if (byte !== IAC) {
        cleaned.push(byte);
        index += 1;
        continue;
      }

      if (index + 1 >= bytes.length) {
        // A lone IAC at the end: the rest is still on its way.
        this.#pending = bytes.slice(index);
        break;
      }

      const command = bytes[index + 1] as number;
      if (command === IAC) {
        // An escaped IAC is one literal byte of the client's own data.
        cleaned.push(IAC);
        index += 2;
        continue;
      }
      if (command === SB) {
        this.#inSubnegotiation = true;
        // Cleared here as well as on completion: finishing one empties it, so
        // this only matters if that ever stops being true.
        this.#subnegotiation = [];
        index += 2;
        continue;
      }
      if (command === WILL || command === WONT || command === DO || command === DONT) {
        if (index + 2 >= bytes.length) {
          // The option byte has not arrived yet.
          this.#pending = bytes.slice(index);
          break;
        }
        reply.push(...this.#handleOption(command, bytes[index + 2] as number));
        index += 3;
        continue;
      }
      // A control the outer gateway already handles. Not application data.
      index += 2;
    }

    return { reply: Uint8Array.from(reply), cleaned: Uint8Array.from(cleaned) };
  }

  /** Read a subnegotiation up to its terminator, if it is all here. */
  #consumeSubnegotiation(bytes: readonly number[], start: number): number {
    let index = start;
    while (index < bytes.length) {
      const byte = bytes[index] as number;
      if (byte !== IAC) {
        this.#append(byte);
        index += 1;
        continue;
      }
      if (index + 1 >= bytes.length) {
        // The byte after IAC decides whether this ends; wait for it.
        this.#pending = bytes.slice(index);
        return bytes.length;
      }
      const next = bytes[index + 1] as number;
      if (next === IAC) {
        this.#append(IAC);
        index += 2;
        continue;
      }
      if (next === SE) {
        this.#inSubnegotiation = false;
        this.#finish();
        return index + 2;
      }
      // An IAC that is neither escaped nor a terminator: not part of the
      // payload, and not something to act on inside one.
      index += 2;
    }
    return index;
  }

  /** Keep a subnegotiation byte, up to the cap. */
  #append(byte: number): void {
    if (this.#subnegotiation.length < MAX_SUBNEGOTIATION_BYTES) {
      // Past the cap the bytes are dropped rather than the connection: a
      // client that never closes one costs a fixed amount either way.
      this.#subnegotiation.push(byte);
    }
  }

  /** Read what a completed subnegotiation said. */
  #finish(): void {
    const payload = this.#subnegotiation;
    this.#subnegotiation = [];
    if (payload.length < 2) {
      // Stated rather than left to the `kind` check below, which would also
      // refuse it — reading past the end happens to give `undefined` here,
      // and that is not a thing to rely on.
      return;
    }
    const option = payload[0] as number;
    const kind = payload[1] as number;
    if (kind !== SUB_IS) {
      return;
    }
    const body = payload.slice(2);
    if (option === OPT_TTYPE) {
      this.#ttypeReceived = true;
      // Lowercased and trimmed on the way in, as the reference stores it, so
      // a client shouting its terminal type is the same client.
      this.term = String.fromCharCode(...body)
        .trim()
        .toLowerCase();
      return;
    }
    if (option === OPT_NEW_ENVIRON) {
      this.#newEnvironReceived = true;
      this.#readEnvironment(body);
    }
  }

  /** Read the variable/value pairs out of an environment subnegotiation. */
  #readEnvironment(body: readonly number[]): void {
    let key: number[] = [];
    let value: number[] = [];
    // Nothing is a variable until a marker says one has started, so bytes
    // before the first marker belong to nobody.
    let started = false;
    let reading: "key" | "value" = "key";

    const store = (): void => {
      if (started && key.length > 0) {
        this.env[String.fromCharCode(...key)] = String.fromCharCode(...value);
      }
    };

    for (let index = 0; index < body.length; index += 1) {
      const byte = body[index] as number;
      if (byte === ENV_VAR || byte === ENV_USERVAR) {
        store();
        key = [];
        value = [];
        started = true;
        reading = "key";
        continue;
      }
      if (byte === ENV_VALUE) {
        reading = "value";
        continue;
      }
      if (byte === ENV_ESC) {
        // The next byte is literal, whatever it would otherwise mean.
        index += 1;
        if (index >= body.length) {
          break;
        }
      }
      const literal = body[index] as number;
      if (reading === "key") {
        key.push(literal);
      } else {
        value.push(literal);
      }
    }
    store();
  }

  /** Answer a verb about an option. */
  #handleOption(verb: number, option: number): number[] {
    // A client accepting one of the two questions is asked to answer it.
    if (verb === WILL && option === OPT_TTYPE) {
      return [...askFor(OPT_TTYPE)];
    }
    if (verb === WILL && option === OPT_NEW_ENVIRON) {
      return [...askFor(OPT_NEW_ENVIRON)];
    }
    // Everything else gets silence — including a client offering something
    // nobody asked for, which RFC 854 says should be refused. Carried over
    // from the reference; recorded in the roadmap rather than corrected,
    // since a port that answered differently would negotiate differently.
    return [];
  }
}
