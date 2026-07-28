//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A full RFC 854 telnet client.
 *
 * Port of the Python module `provide.uterm.transports.telnet_transport`.
 *
 * The framing lives in `./telnet.ts`; this is the conversation. A client that
 * opens without offering anything gets a line-mode, non-binary session and a
 * BBS then draws for the wrong terminal — so the offer goes out first, and
 * each answer that agrees to something is followed immediately by the value
 * the far end needs (the window size, the terminal type), because agreeing
 * and then staying silent leaves the server guessing.
 */

import { type ConnectionTransport, TransportConnectionError } from "./base.ts";
import { escapeTelnetData, TELNET, TelnetBuffer } from "./telnet.ts";

/** The socket this transport drives. */
export interface TelnetSocket {
  /** Whether the socket is closing or closed. */
  readonly closing: boolean;
  /** Up to `maxBytes`; an empty result means the far end has gone. */
  read(maxBytes: number): Promise<Uint8Array>;
  /** Put bytes on the wire. */
  write(data: Uint8Array): Promise<void>;
  /** Close it. */
  close(): Promise<void>;
  /** The address actually reached, for the post-connect egress check. */
  peerAddress(): string | undefined;
}

/** How a socket is opened. */
export type TelnetConnect = (host: string, port: number, timeoutMs: number) => Promise<TelnetSocket>;

/** Options for {@link TelnetTransport}. */
export interface TelnetTransportOptions {
  /** Opens the socket. Injected, so a test needs no network. */
  connect: TelnetConnect;
}

/** Per-connection options. */
export interface TelnetConnectOptions {
  /** Terminal width, offered through NAWS. */
  cols?: number;
  /** Terminal height, offered through NAWS. */
  rows?: number;
  /** Terminal type, offered through TTYPE. */
  term?: string;
  /** How long to wait for the connection. */
  timeoutS?: number;
}

/** How long a connection attempt is given. */
export const TELNET_CONNECT_TIMEOUT_S = 30.0;

/**
 * How much unconsumed input is tolerated.
 *
 * A subnegotiation has no length bound, so an upstream that sends `IAC SB`
 * and never `IAC SE` would grow this without limit. The cap is far above any
 * legitimate one; reaching it means the peer is broken or hostile.
 */
export const TELNET_MAX_RX_BUFFER = 256 * 1024;

/** Terminal geometry offered when the caller names none. */
export const TELNET_TRANSPORT_DEFAULTS = { cols: 80, rows: 25, term: "ANSI" } as const;

/** The subnegotiation byte that says "this is my terminal type". */
const TTYPE_IS = 0;

/** The subnegotiation byte that asks for it. */
const TTYPE_SEND = 1;

/** Backspace, which a BBS deletes with. */
const BACKSPACE = 0x08;

/** Delete, which a browser terminal sends for the backspace key. */
const DELETE = 0x7f;

/** Which of the four commands an option has already been sent. */
type NegotiationSide = "will" | "wont" | "do" | "dont";

/** A full RFC 854 telnet client behind the transport interface. */
export class TelnetTransport implements ConnectionTransport {
  readonly #connect: TelnetConnect;
  #socket: TelnetSocket | undefined;
  #buffer = new TelnetBuffer();
  #pending = 0;
  #cols: number = TELNET_TRANSPORT_DEFAULTS.cols;
  #rows: number = TELNET_TRANSPORT_DEFAULTS.rows;
  #term: string = TELNET_TRANSPORT_DEFAULTS.term;
  /**
   * What has already been said about each option.
   *
   * Shared between what arrives and what is sent, exactly as the reference
   * does it: an incoming `WILL X` records `will[X]`, so this end will not
   * then send its own `WILL X`. That is what stops two polite implementations
   * negotiating at each other forever.
   */
  readonly #negotiated: Record<NegotiationSide, Set<number>> = {
    will: new Set(),
    wont: new Set(),
    do: new Set(),
    dont: new Set(),
  };

  constructor(options: TelnetTransportOptions) {
    this.#connect = options.connect;
  }

  /**
   * Open the connection and make the opening offer.
   *
   * @throws {TransportConnectionError} If the socket cannot be opened.
   */
  async connect(host: string, port: number, options: TelnetConnectOptions = {}): Promise<void> {
    if (this.#socket !== undefined) {
      await this.disconnect();
    }
    const timeoutS = options.timeoutS ?? TELNET_CONNECT_TIMEOUT_S;
    try {
      this.#socket = await this.#connect(host, port, timeoutS * 1000);
    } catch (error) {
      throw new TransportConnectionError(`Failed to connect to ${host}:${port}`, { cause: error });
    }
    this.#buffer = new TelnetBuffer();
    this.#pending = 0;
    this.#cols = options.cols ?? TELNET_TRANSPORT_DEFAULTS.cols;
    this.#rows = options.rows ?? TELNET_TRANSPORT_DEFAULTS.rows;
    this.#term = options.term ?? TELNET_TRANSPORT_DEFAULTS.term;
    // Binary and suppress-go-ahead, unasked: without them the session is
    // line-mode and seven-bit, and a BBS draws for the wrong terminal.
    await this.#sendOnce("will", TELNET.OPT_BINARY);
    await this.#sendOnce("will", TELNET.OPT_SGA);
  }

  /** Close the connection. Safe to call twice. */
  async disconnect(): Promise<void> {
    const socket = this.#socket;
    if (socket === undefined) {
      return;
    }
    this.#socket = undefined;
    try {
      await socket.close();
    } catch {
      // Cleanup that raised would leave the transport wedged as connected.
    }
  }

  /**
   * Send bytes, escaped for the wire.
   *
   * @throws {TransportConnectionError} If not connected, or the far end went
   *   mid-send.
   */
  async send(data: Uint8Array): Promise<void> {
    const socket = this.#requireSocket();
    // A browser terminal sends DEL for the backspace key; a BBS deletes with
    // backspace, so a literal DEL prints as a stray character instead.
    const remapped = Uint8Array.from(data, (byte) => (byte === DELETE ? BACKSPACE : byte));
    try {
      await socket.write(escapeTelnetData(remapped));
    } catch (error) {
      await this.disconnect();
      throw new TransportConnectionError("Connection lost", { cause: error });
    }
  }

  /**
   * Read application bytes, answering any negotiation that arrives.
   *
   * @throws {TransportConnectionError} If not connected, the far end closed
   *   with nothing left to hand over, or the peer sent more unconsumed input
   *   than {@link TELNET_MAX_RX_BUFFER}.
   */
  async receive(maxBytes: number, timeoutMs: number): Promise<Uint8Array> {
    const socket = this.#requireSocket();
    let chunk: Uint8Array | typeof TIMED_OUT;
    try {
      chunk = await this.#readWithTimeout(socket, maxBytes, timeoutMs);
    } catch (error) {
      await this.disconnect();
      throw new TransportConnectionError("Connection lost", { cause: error });
    }
    if (chunk === TIMED_OUT) {
      // A quiet terminal is not a broken one.
      return new Uint8Array(0);
    }

    if (chunk.length === 0) {
      // The far end has gone. Anything held back as an incomplete sequence is
      // handed over rather than dropped, and only then is the connection torn
      // down.
      const flushed = this.#buffer.flush();
      await this.disconnect();
      if (flushed.payload.length > 0) {
        return flushed.payload;
      }
      throw new TransportConnectionError("Connection closed by remote");
    }

    const result = this.#buffer.feed(chunk);
    this.#pending = this.#buffer.pending;
    if (this.#pending > TELNET_MAX_RX_BUFFER) {
      this.#buffer = new TelnetBuffer();
      this.#pending = 0;
      throw new TransportConnectionError(
        `telnet receive buffer exceeded ${TELNET_MAX_RX_BUFFER} bytes ` +
          "(likely IAC SB without IAC SE) — closing connection",
      );
    }
    // Answered before returning rather than in a background task: the bytes
    // that reach the wire are the same either way, and doing it here means a
    // caller that reads once has already replied by the time it looks.
    for (const event of result.events) {
      if (event.kind === "negotiate") {
        await this.#answerNegotiation(event.command, event.option);
      } else {
        await this.#answerSubnegotiation(event.payload);
      }
    }
    return result.payload;
  }

  /** Whether the connection is live. */
  isConnected(): boolean {
    return this.#socket !== undefined && !this.#socket.closing;
  }

  /**
   * The address actually reached.
   *
   * The real peer, not the hostname asked for, so an egress check runs
   * against what the connection went to. Absent means "proceed": a caller
   * must not fail closed on a socket that simply cannot say.
   */
  peerIp(): string | undefined {
    return this.#socket?.peerAddress();
  }

  /**
   * Tell the far end the terminal changed size.
   *
   * @throws {TransportConnectionError} If not connected.
   */
  async setSize(cols: number, rows: number): Promise<void> {
    this.#requireSocket();
    this.#cols = cols;
    this.#rows = rows;
    await this.#sendNaws(cols, rows);
  }

  /** Read, or give up after `timeoutMs`. */
  async #readWithTimeout(
    socket: TelnetSocket,
    maxBytes: number,
    timeoutMs: number,
  ): Promise<Uint8Array | typeof TIMED_OUT> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<typeof TIMED_OUT>((resolve) => {
      timer = setTimeout(() => resolve(TIMED_OUT), timeoutMs);
    });
    try {
      return await Promise.race([socket.read(maxBytes), timeout]);
    } finally {
      clearTimeout(timer);
    }
  }

  /** The live socket, or a refusal. */
  #requireSocket(): TelnetSocket {
    if (this.#socket === undefined) {
      throw new TransportConnectionError("Not connected");
    }
    return this.#socket;
  }

  /**
   * Answer one negotiation.
   *
   * The reference guards against the socket having gone, because it dispatches
   * the answer as a background task that may run after a disconnect. Answering
   * inline means that cannot happen here, so there is no guard to write — the
   * write itself still checks, for the same reason.
   */
  async #answerNegotiation(command: number, option: number): Promise<void> {
    // What arrives is recorded in the same table the sends check, so this end
    // does not answer an option the far end has already settled.
    this.#record(command, option);
    if (command === TELNET.DO) {
      await this.#answerDo(option);
      return;
    }
    if (command === TELNET.WILL) {
      // Echo, suppress-go-ahead and binary are the three worth having.
      await this.#sendOnce(
        option === TELNET.OPT_ECHO || option === TELNET.OPT_SGA || option === TELNET.OPT_BINARY ? "do" : "dont",
        option,
      );
      return;
    }
    // A refusal in either direction is answered with the matching refusal.
    await this.#sendOnce(command === TELNET.DONT ? "wont" : "dont", option);
  }

  /** Answer a DO, sending the value where the option needs one. */
  async #answerDo(option: number): Promise<void> {
    if (option === TELNET.OPT_BINARY || option === TELNET.OPT_SGA) {
      await this.#sendOnce("will", option);
      return;
    }
    if (option === TELNET.OPT_NAWS) {
      await this.#sendOnce("will", option);
      // Agreeing and then never saying how big leaves the server guessing.
      await this.#sendNaws(this.#cols, this.#rows);
      return;
    }
    if (option === TELNET.OPT_TTYPE) {
      await this.#sendOnce("will", option);
      await this.#sendTtype(this.#term);
      return;
    }
    await this.#sendOnce("wont", option);
  }

  /** Answer a subnegotiation, which in practice is a terminal-type request. */
  async #answerSubnegotiation(payload: Uint8Array): Promise<void> {
    if (payload.length > 1 && payload[0] === TELNET.OPT_TTYPE && payload[1] === TTYPE_SEND) {
      await this.#sendTtype(this.#term);
    }
  }

  /** Record what has been said about an option. */
  #record(command: number, option: number): void {
    const side =
      command === TELNET.DO ? "do" : command === TELNET.DONT ? "dont" : command === TELNET.WILL ? "will" : "wont";
    this.#negotiated[side].add(option);
  }

  /**
   * Send a command for an option, at most once.
   *
   * Two polite implementations that answered every message would negotiate at
   * each other forever.
   */
  async #sendOnce(side: NegotiationSide, option: number): Promise<void> {
    if (this.#negotiated[side].has(option)) {
      return;
    }
    const command =
      side === "will" ? TELNET.WILL : side === "wont" ? TELNET.WONT : side === "do" ? TELNET.DO : TELNET.DONT;
    await this.#writeRaw(Uint8Array.from([TELNET.IAC, command, option]));
    this.#negotiated[side].add(option);
  }

  /** Send the window size. */
  async #sendNaws(cols: number, rows: number): Promise<void> {
    const size = Uint8Array.from([(cols >> 8) & 0xff, cols & 0xff, (rows >> 8) & 0xff, rows & 0xff]);
    await this.#writeSubnegotiation(TELNET.OPT_NAWS, size);
  }

  /** Send the terminal type. */
  async #sendTtype(term: string): Promise<void> {
    const name = Uint8Array.from(term, (character) => character.charCodeAt(0) & 0xff);
    await this.#writeSubnegotiation(TELNET.OPT_TTYPE, Uint8Array.from([TTYPE_IS, ...name]));
  }

  /** Frame and send a subnegotiation. */
  async #writeSubnegotiation(option: number, payload: Uint8Array): Promise<void> {
    // Every 0xFF inside the payload is doubled, or the receiver reads it as
    // the framing byte and the block ends in the wrong place.
    await this.#writeRaw(
      Uint8Array.from([TELNET.IAC, TELNET.SB, option, ...escapeTelnetData(payload), TELNET.IAC, TELNET.SE]),
    );
  }

  /** Write without escaping — these are commands, not data. */
  async #writeRaw(data: Uint8Array): Promise<void> {
    const socket = this.#socket;
    if (socket === undefined || socket.closing) {
      return;
    }
    try {
      await socket.write(data);
    } catch {
      // A negotiation that cannot be sent is not worth failing the read that
      // triggered it; the next operation will find the socket gone.
    }
  }
}

/**
 * Distinguishes a timed-out read from an empty one.
 *
 * They mean opposite things — nothing has arrived yet, versus the far end has
 * gone — so they cannot both be an empty buffer.
 */
const TIMED_OUT = Symbol("timed out");
