//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a worker talks to a tunnel through.
 *
 * Port of `provide.uterm.tunnel.client`. What this puts on the wire is the
 * whole contract: an `open` naming the terminal's size, data frames on a
 * channel, a resize, an end-of-file, and a reconnect that backs off.
 *
 * * **Nothing is sent before there is a connection.** Every call refuses
 *   rather than queueing, because a frame silently dropped looks to a caller
 *   exactly like one delivered and ignored.
 * * **The backoff saturates.** A client that kept doubling would eventually
 *   stop retrying at all; one that never backed off would be attacking its
 *   own server.
 */

import { CHANNEL_DATA, decodeFrame, encodeControl, encodeFrame, FLAG_EOF, type TunnelFrame } from "../tunnel/index.ts";

/** How long to wait before each attempt, saturating at the last. */
export const BACKOFF_SCHEDULE: readonly number[] = [1, 2, 5, 10, 30];

/** The channel a terminal's own traffic uses. */
export const TERMINAL_CHANNEL = 1;

/** As much of a socket as the client needs. */
export interface TunnelSocket {
  send(data: Uint8Array): Promise<void>;
  recv(): Promise<Uint8Array | string>;
  close(): Promise<void>;
  /** Whether the connection is still up. */
  isOpen(): boolean;
}

/** Told about an attempt that failed, so a reconnect is not silent. */
export interface TunnelClientLogger {
  reconnectFailed(attempt: number, delayS: number): void;
}

/** A logger that says nothing. */
const SILENT: TunnelClientLogger = { reconnectFailed: () => {} };

/** What the client is built with. */
export interface TunnelClientOptions {
  url: string;
  token: string;
  /** Opens a socket. Called once per connect, so a retry gets a fresh one. */
  connect(url: string, headers: Record<string, string>): Promise<TunnelSocket>;
  /** How a backoff is taken. Injected so a test need not spend it. */
  sleep?: (seconds: number) => Promise<void>;
  logger?: TunnelClientLogger;
}

/** The wait before attempt `attempt`, counting from zero. */
export function backoffFor(attempt: number): number {
  // Saturating rather than growing: past the end of the schedule every
  // further attempt waits the longest step.
  const index = Math.min(Math.max(0, Math.trunc(attempt)), BACKOFF_SCHEDULE.length - 1);
  return BACKOFF_SCHEDULE[index] as number;
}

/** Wait for `seconds`. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, seconds * 1000);
  });
}

/** A tunnel, from the worker's end. */
export class TunnelClient {
  readonly #options: TunnelClientOptions;
  #socket: TunnelSocket | undefined;

  constructor(options: TunnelClientOptions) {
    this.#options = options;
  }

  /** Whether there is a connection to send on. */
  get connected(): boolean {
    return this.#socket?.isOpen() ?? false;
  }

  /**
   * Open the connection.
   *
   * The token travels as a bearer header rather than in the URL, so it does
   * not end up in anybody's access log.
   */
  async connect(): Promise<void> {
    this.#socket = await this.#options.connect(this.#options.url, {
      Authorization: `Bearer ${this.#options.token}`,
    });
  }

  /**
   * Close it.
   *
   * Idempotent, and the socket is forgotten before it is closed, so a close
   * that fails still leaves the client disconnected rather than holding a
   * socket nobody can use.
   */
  async close(): Promise<void> {
    const socket = this.#socket;
    this.#socket = undefined;
    if (socket !== undefined) {
      await socket.close();
    }
  }

  /** Ask for a terminal of a given size. */
  async openTerminal(cols: number, rows: number): Promise<void> {
    await this.#send(
      encodeControl({ type: "open", channel: TERMINAL_CHANNEL, tunnel_type: "terminal", term_size: [cols, rows] }),
    );
  }

  /** Send terminal bytes. */
  async sendData(data: Uint8Array, channel: number = CHANNEL_DATA): Promise<void> {
    await this.#send(encodeFrame(channel, data));
  }

  /** Say the terminal changed size. */
  async sendResize(cols: number, rows: number): Promise<void> {
    await this.#send(encodeControl({ type: "resize", channel: TERMINAL_CHANNEL, cols, rows }));
  }

  /** Say there is no more to come on a channel. */
  async sendEof(channel: number = CHANNEL_DATA): Promise<void> {
    await this.#send(encodeFrame(channel, new Uint8Array(), FLAG_EOF));
  }

  /**
   * Read one frame.
   *
   * @throws {Error} When there is no connection to read from.
   */
  async recv(): Promise<TunnelFrame> {
    const socket = this.#require();
    const raw = await socket.recv();
    // A socket that hands over text is handing over the same bytes: each
    // code unit is one byte, as the reference's latin-1 decode assumes.
    return decodeFrame(typeof raw === "string" ? Uint8Array.from([...raw].map((c) => c.charCodeAt(0))) : raw);
  }

  /**
   * Keep trying to connect, backing off between attempts.
   *
   * @param maxAttempts How many times to try. Zero means until it works.
   * @returns Whether it connected.
   */
  async reconnectLoop(maxAttempts = 0): Promise<boolean> {
    const sleep = this.#options.sleep ?? realSleep;
    const logger = this.#options.logger ?? SILENT;
    // Unlimited is a number nobody reaches rather than a different loop, as
    // the reference writes it.
    const limit = maxAttempts > 0 ? maxAttempts : Number.POSITIVE_INFINITY;

    for (let attempt = 0; attempt < limit; attempt += 1) {
      const delay = backoffFor(attempt);
      // Before the attempt, not after: a client reconnecting into a server
      // that has just fallen over should not hit it the instant it notices.
      await sleep(delay);
      try {
        await this.connect();
        return true;
      } catch {
        logger.reconnectFailed(attempt + 1, delay);
      }
    }
    return false;
  }

  /** Send raw bytes, refusing when there is nothing to send them on. */
  async #send(data: Uint8Array): Promise<void> {
    await this.#require().send(data);
  }

  /**
   * The socket, or a refusal.
   *
   * @throws {Error} `not connected` — said rather than queued, since a frame
   *   silently dropped looks exactly like one delivered and ignored.
   */
  #require(): TunnelSocket {
    if (this.#socket === undefined) {
      throw new Error("not connected");
    }
    return this.#socket;
  }
}
