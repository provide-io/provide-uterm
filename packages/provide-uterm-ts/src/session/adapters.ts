//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Telnet and websocket sessions.
 *
 * Port of the Python modules `provide.uterm.telnet_session` and
 * `provide.uterm.ws_session`.
 *
 * Both are thin over {@link TransportSession}; what they actually contribute
 * is the encodings, and those are not cosmetic. A BBS expects CP437 on the
 * wire, so sending an accented character as UTF-8 puts two bytes where the
 * server wanted one and the screen desynchronises from there on.
 */

import { decodeCp437, encodeCp437 } from "../screen/index.ts";
import { type SessionTransport, TransportSession } from "./transport-session.ts";

/** Codecs a session can speak on the wire. */
export type SessionEncoding = "cp437" | "latin-1";

/**
 * Defaults for a telnet session.
 *
 * CP437 both ways: it is what a BBS draws its interface in, and what it
 * expects back.
 */
export const TELNET_SESSION_DEFAULTS = {
  cols: 80,
  rows: 25,
  term: "ANSI",
  connectTimeoutS: 30,
  receiveEncoding: "cp437",
  /** Off by default, so every byte reaches the screen as a plain client. */
  controlFrames: false,
} as const satisfies {
  cols: number;
  rows: number;
  term: string;
  connectTimeoutS: number;
  receiveEncoding: SessionEncoding;
  controlFrames: boolean;
};

/**
 * Defaults for a websocket session.
 *
 * The two encodings answer different questions. A binary frame carries the
 * bytes a BBS drew, so it is CP437. A text frame already carries characters,
 * and latin-1 is the identity that turns them back into the bytes they stood
 * for.
 *
 * The keepalives matter for a passive worker: it emits nothing for minutes at
 * a time, and without pings the socket is reaped by whatever sits in the
 * middle.
 */
export const WEBSOCKET_SESSION_DEFAULTS = {
  cols: 80,
  rows: 25,
  pingIntervalS: 20,
  pingTimeoutS: 20,
  closeTimeoutS: 10,
  receiveEncoding: "cp437",
  textFrameEncoding: "latin-1",
  controlFrames: false,
} as const satisfies {
  cols: number;
  rows: number;
  pingIntervalS: number;
  pingTimeoutS: number;
  closeTimeoutS: number;
  receiveEncoding: SessionEncoding;
  textFrameEncoding: SessionEncoding;
  controlFrames: boolean;
};

/**
 * Encode text for the wire.
 *
 * A character CP437 cannot carry becomes a question mark, matching CPython's
 * replace policy — a visible placeholder beats an exception mid-stream.
 */
export function encodeSessionText(text: string, encoding: SessionEncoding): Uint8Array {
  if (encoding === "cp437") {
    return encodeCp437(text);
  }
  const bytes = new Uint8Array(text.length);
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    bytes[index] = code > 0xff ? 0x3f : code;
  }
  return bytes;
}

/** Decode bytes from the wire. */
export function decodeSessionText(data: Uint8Array, encoding: SessionEncoding): string {
  if (encoding === "cp437") {
    return decodeCp437(data);
  }
  let out = "";
  for (const byte of data) {
    out += String.fromCharCode(byte);
  }
  return out;
}

/** Options for {@link TelnetSession}. */
export interface TelnetSessionOptions {
  /** The connected transport this session drives. */
  transport: SessionTransport;
  /** Host, kept for diagnostics and error messages. */
  host: string;
  /** Port, kept for diagnostics and error messages. */
  port: number;
  /** Terminal width. */
  cols?: number;
  /** Terminal height. */
  rows?: number;
  /** Terminal type advertised during negotiation. */
  term?: string;
  /** How long to wait for the connection. */
  connectTimeoutS?: number;
  /** Codec for incoming terminal bytes. */
  receiveEncoding?: SessionEncoding;
  /** Whether inline control frames are parsed out of the stream. */
  controlFrames?: boolean;
}

/** Options for {@link WebSocketSession}. */
export interface WebSocketSessionOptions {
  /** The connected transport this session drives. */
  transport: SessionTransport;
  /** Where it connects, kept for diagnostics. */
  url: string;
  /** Terminal width. */
  cols?: number;
  /** Terminal height. */
  rows?: number;
  /** Codec for incoming terminal bytes. */
  receiveEncoding?: SessionEncoding;
  /** Codec for a websocket text frame. */
  textFrameEncoding?: SessionEncoding;
  /** Whether inline control frames are parsed out of the stream. */
  controlFrames?: boolean;
}

/** A telnet transport with terminal emulation. */
export class TelnetSession extends TransportSession {
  /** Host this session connects to. */
  readonly host: string;
  /** Port this session connects to. */
  readonly port: number;
  /** Terminal type advertised during negotiation. */
  readonly term: string;
  /** How long to wait for the connection. */
  readonly connectTimeoutS: number;
  /** The bytes the last send put on the wire, for diagnostics. */
  lastSentBytes: Uint8Array | undefined;

  readonly #receiveEncoding: SessionEncoding;

  constructor(options: TelnetSessionOptions) {
    super({
      transport: options.transport,
      cols: options.cols ?? TELNET_SESSION_DEFAULTS.cols,
      rows: options.rows ?? TELNET_SESSION_DEFAULTS.rows,
      controlChannel: options.controlFrames ?? TELNET_SESSION_DEFAULTS.controlFrames,
    });
    this.host = options.host;
    this.port = options.port;
    this.term = options.term ?? TELNET_SESSION_DEFAULTS.term;
    this.connectTimeoutS = options.connectTimeoutS ?? TELNET_SESSION_DEFAULTS.connectTimeoutS;
    this.#receiveEncoding = options.receiveEncoding ?? TELNET_SESSION_DEFAULTS.receiveEncoding;
  }

  /** Send text, encoded the way a BBS expects to read it. */
  override async send(data: string): Promise<void> {
    const bytes = encodeSessionText(data, "cp437");
    this.lastSentBytes = bytes;
    await super.send(decodeSessionText(bytes, "latin-1"));
  }

  /** Decode terminal bytes with this session's receive codec. */
  decode(data: Uint8Array): string {
    return decodeSessionText(data, this.#receiveEncoding);
  }
}

/** A websocket transport with terminal emulation. */
export class WebSocketSession extends TransportSession {
  /** Where this session connects. */
  readonly url: string;

  readonly #receiveEncoding: SessionEncoding;
  readonly #textFrameEncoding: SessionEncoding;

  constructor(options: WebSocketSessionOptions) {
    super({
      transport: options.transport,
      cols: options.cols ?? WEBSOCKET_SESSION_DEFAULTS.cols,
      rows: options.rows ?? WEBSOCKET_SESSION_DEFAULTS.rows,
      controlChannel: options.controlFrames ?? WEBSOCKET_SESSION_DEFAULTS.controlFrames,
    });
    this.url = options.url;
    this.#receiveEncoding = options.receiveEncoding ?? WEBSOCKET_SESSION_DEFAULTS.receiveEncoding;
    this.#textFrameEncoding = options.textFrameEncoding ?? WEBSOCKET_SESSION_DEFAULTS.textFrameEncoding;
  }

  /** Decode a binary frame's terminal bytes. */
  decode(data: Uint8Array): string {
    return decodeSessionText(data, this.#receiveEncoding);
  }

  /**
   * Reinterpret a text frame.
   *
   * A different question from decoding a binary one: the frame already
   * carries characters, so this turns them back into the bytes they stood
   * for rather than re-decoding a codepage.
   */
  decodeTextFrame(text: string): string {
    return decodeSessionText(encodeSessionText(text, this.#textFrameEncoding), this.#textFrameEncoding);
  }
}
