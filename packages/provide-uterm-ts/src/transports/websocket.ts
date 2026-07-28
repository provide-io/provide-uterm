//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * WebSocket client transport.
 *
 * Port of the Python module `provide.uterm.transports.ws_transport`.
 *
 * Two decisions here are silent when wrong. Outgoing bytes go out as a TEXT
 * frame, because a BINARY frame reaches the Cloudflare Worker as a `JsProxy`
 * and is dropped without an error — the session just goes quiet. And an
 * incoming TEXT frame is turned back into terminal bytes using the target's
 * wire dialect, which defaults to latin-1: byte-oriented BBS gateways place
 * each terminal byte in the same-valued code point, so a UTF-8 reading there
 * would fill the screen with replacement characters.
 */

import { type ConnectionTransport, TransportConnectionError } from "./base.ts";

/** The state of the underlying socket. */
export type SocketState = "connecting" | "open" | "closing" | "closed";

/** Signals that the far end closed. */
export class WebSocketClosedError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "WebSocketClosedError";
  }
}

/** The socket this transport drives. */
export interface WebSocketLike {
  /** Whether the connection is open, closing, or already gone. */
  readonly state: SocketState;
  /** Send one frame — a string is TEXT, bytes are BINARY. */
  send(message: string | Uint8Array): Promise<void>;
  /** The next whole message. */
  recv(): Promise<string | Uint8Array>;
  /** Close the connection. */
  close(): Promise<void>;
}

/** How the transport opens a socket. */
export type WebSocketConnect = (url: string, options: Record<string, unknown>) => Promise<WebSocketLike>;

/** Options for {@link WebSocketTransport}. */
export interface WebSocketTransportOptions {
  /** Opens the socket. Injected, so the transport needs no global client. */
  connect: WebSocketConnect;
  /** The wire dialect of an incoming text frame. */
  textFrameEncoding?: "latin-1" | "utf-8";
}

/** Per-connection options. */
export interface WebSocketConnectOptions {
  // Each option admits an explicit `undefined`, because the reference sorts
  // set from unset itself: a caller forwarding an optional config value
  // should not have to build the object conditionally to do so.
  /** The full URL. Wins over `host` and `port` when non-empty. */
  url?: string | undefined;
  /** Largest message to accept. */
  maxSize?: number | undefined;
  /** How often to send a keepalive; zero turns them off. */
  pingInterval?: number | undefined;
  /** How long to wait for a keepalive reply. */
  pingTimeout?: number | undefined;
  /** How long to wait for a close handshake. */
  closeTimeout?: number | undefined;
  /** Origin to present — a worker that gates cross-origin upgrades needs it. */
  origin?: string | undefined;
  /** Extra request headers. */
  additionalHeaders?: Record<string, string> | undefined;
}

/**
 * The options passed through to the socket, in order.
 *
 * Only these are forwarded, so an option the client does not know is dropped
 * rather than reaching the socket as a surprise.
 */
const FORWARDED = [
  "maxSize",
  "pingInterval",
  "pingTimeout",
  "closeTimeout",
  "origin",
  "additionalHeaders",
] as const satisfies ReadonlyArray<keyof WebSocketConnectOptions>;

/** Latin-1's substitute for a code point that does not fit. */
const SUBSTITUTE = 0x3f;

const encoder = new TextEncoder();
// Non-fatal, so a bad byte becomes a replacement character instead of
// throwing: one byte from a noisy line must not tear down the session.
const decoder = new TextDecoder("utf-8");

/** Encode text as terminal bytes in the given wire dialect. */
function encodeFrame(text: string, encoding: "latin-1" | "utf-8"): Uint8Array {
  if (encoding === "utf-8") {
    return encoder.encode(text);
  }
  const bytes = new Uint8Array(text.length);
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    // Substitution rather than a raise: a single undrawable glyph must not
    // kill the session.
    bytes[index] = code > 0xff ? SUBSTITUTE : code;
  }
  return bytes;
}

/** A WebSocket client behind the transport interface. */
export class WebSocketTransport implements ConnectionTransport {
  readonly #connect: WebSocketConnect;
  readonly #textFrameEncoding: "latin-1" | "utf-8";
  #url: string | undefined;
  #socket: WebSocketLike | undefined;
  #connected = false;

  constructor(options: WebSocketTransportOptions) {
    this.#connect = options.connect;
    this.#textFrameEncoding = options.textFrameEncoding ?? "latin-1";
  }

  /**
   * Open the connection.
   *
   * A non-empty `url` wins; otherwise a secure URL is built from `host` and
   * `port`, so a session that passed neither is never silently downgraded.
   *
   * @throws {TransportConnectionError} Carrying the underlying failure as its
   *   cause — "failed to connect" alone leaves an operator unable to tell DNS
   *   from a refused port from TLS.
   */
  async connect(host: string, port: number, options: WebSocketConnectOptions = {}): Promise<void> {
    this.#url = options.url || `wss://${host}:${port}`;

    const forwarded: Record<string, unknown> = {};
    for (const key of FORWARDED) {
      const value = options[key];
      // Only an unset option is dropped: zero is a meaningful ping interval.
      if (value !== undefined) {
        forwarded[key] = value;
      }
    }

    try {
      this.#socket = await this.#connect(this.#url, forwarded);
      this.#connected = true;
    } catch (error) {
      this.#connected = false;
      throw new TransportConnectionError(`Failed to connect to ${this.#url}`, { cause: error });
    }
  }

  /** Close the connection. Safe to call twice, and on a socket that is gone. */
  async disconnect(): Promise<void> {
    // Clearing the flag here is not observable on its own — the socket goes
    // in the same breath, and every guard checks both. It is kept because the
    // pairing is what makes the *failed reconnect* case safe, where the flag
    // is cleared and the old socket is not.
    this.#connected = false;
    const socket = this.#socket;
    if (socket !== undefined) {
      this.#socket = undefined;
      try {
        await socket.close();
      } catch {
        // Cleanup that raised would otherwise leave the transport wedged as
        // connected, with nothing able to reach the far end.
      }
    }
  }

  /**
   * Send bytes as a TEXT frame.
   *
   * The bytes are decoded back to text first: a BINARY frame is dropped by
   * the worker without an error.
   *
   * @throws {TransportConnectionError} If not connected, or the far end
   *   closed mid-send — in which case the connection is torn down first, so a
   *   caller cannot keep writing into a socket that is already gone.
   */
  async send(data: Uint8Array): Promise<void> {
    const socket = this.#requireSocket();
    try {
      await socket.send(decoder.decode(data));
    } catch (error) {
      if (error instanceof WebSocketClosedError) {
        await this.disconnect();
        throw new TransportConnectionError("Connection closed", { cause: error });
      }
      throw error;
    }
  }

  /**
   * Read the next message.
   *
   * `maxBytes` is advisory and ignored: WebSocket is message-framed, so
   * chunking to it would corrupt the framing.
   *
   * @throws {TransportConnectionError} If not connected, or the connection is
   *   lost. A read timeout is not a loss — it returns empty and leaves the
   *   connection up, because a quiet terminal is not a broken one.
   */
  async receive(_maxBytes: number, timeoutMs: number): Promise<Uint8Array> {
    const socket = this.#requireSocket();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<typeof TIMED_OUT>((resolve) => {
      timer = setTimeout(() => resolve(TIMED_OUT), timeoutMs);
    });
    try {
      const message = await Promise.race([socket.recv(), timeout]);
      if (message === TIMED_OUT) {
        return new Uint8Array(0);
      }
      // A text frame is terminal bytes in the target's wire dialect; a binary
      // frame already is them.
      return typeof message === "string" ? encodeFrame(message, this.#textFrameEncoding) : message;
    } catch (error) {
      await this.disconnect();
      if (error instanceof WebSocketClosedError) {
        throw new TransportConnectionError("Connection closed", { cause: error });
      }
      throw new TransportConnectionError("WebSocket receive error", { cause: error });
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Whether the connection is live.
   *
   * Read from the socket rather than only from the transport's own flag: the
   * far end can go without the transport being told.
   */
  isConnected(): boolean {
    return this.#connected && this.#socket !== undefined && this.#socket.state === "open";
  }

  /** The live socket, or a refusal. */
  #requireSocket(): WebSocketLike {
    if (!this.#connected || this.#socket === undefined) {
      throw new TransportConnectionError("Not connected");
    }
    return this.#socket;
  }
}

/** Distinguishes a timed-out read from a message, including an empty one. */
const TIMED_OUT = Symbol("timed out");
