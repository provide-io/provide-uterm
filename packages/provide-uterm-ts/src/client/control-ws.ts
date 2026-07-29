//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A socket that carries terminal bytes and control frames together.
 *
 * Port of `provide.uterm.client.control_ws`. Both travel over one connection,
 * so which of the two a payload becomes is what decides whether a session
 * works:
 *
 * * **A logical frame is encoded by its type.** `input` and `term` are
 *   terminal bytes and are escaped as such; everything else is a control
 *   frame. A control frame sent as terminal bytes would be typed into
 *   somebody's shell, and terminal bytes sent as a control frame would hand
 *   the far end something to act on.
 * * **The name the bytes get back depends on who is listening.** A worker
 *   reads them as `input` — somebody typing at it — and a browser as `term` —
 *   the session printing.
 * * **A bare JSON object is refused.** It looks like a control frame to
 *   whoever reads the code and is not one to the codec, so sending it loses
 *   the frame silently.
 */

import {
  type ControlFrameChunk,
  ControlFrameDecoder,
  encodeControlFrame,
  encodeTerminalData,
} from "../control-channel/index.ts";

/** Which end of the connection this is. */
export type WsRole = "browser" | "worker";

/** A logical frame: terminal bytes, or something to act on. */
export type LogicalFrame = Record<string, unknown>;

/** The types that mean "these are terminal bytes". */
const TERMINAL_TYPES = new Set(["input", "term"]);

/**
 * Encode one logical frame for the wire.
 *
 * The type decides: matched exactly, so a frame typed `INPUT` is a control
 * frame rather than keystrokes.
 */
export function encodeLogicalFrame(payload: LogicalFrame): string {
  if (typeof payload.type === "string" && TERMINAL_TYPES.has(payload.type)) {
    // The reference stringifies whatever `data` holds, and an absent one
    // becomes empty rather than the word "undefined".
    return encodeTerminalData(payload.data === undefined ? "" : String(payload.data));
  }
  return encodeControlFrame(payload);
}

/** Turn the inline stream back into logical frames. */
export class LogicalFrameDecoder {
  readonly #role: WsRole;
  readonly #decoder = new ControlFrameDecoder();

  constructor(role: WsRole) {
    this.#role = role;
  }

  /** The name terminal bytes get, which depends on the direction they travel. */
  dataType(): string {
    return this.#role === "worker" ? "input" : "term";
  }

  /** Frames completed by this chunk. */
  feed(raw: string): LogicalFrame[] {
    return this.#map(this.#decoder.feed(raw));
  }

  /** Whatever is left once the stream has ended. */
  finish(): LogicalFrame[] {
    return this.#map(this.#decoder.finish());
  }

  #map(chunks: readonly ControlFrameChunk[]): LogicalFrame[] {
    return chunks.map((chunk) =>
      chunk.kind === "control" ? chunk.control : { type: this.dataType(), data: chunk.data },
    );
  }
}

/** As much of a socket as this client needs. */
export interface InlineSocket {
  send(data: string | Uint8Array): Promise<void>;
  recv(): Promise<string | Uint8Array>;
}

/** Whether a value is a plain object, which is what a frame is. */
function isFrame(value: unknown): value is LogicalFrame {
  return typeof value === "object" && value !== null && !Array.isArray(value) && !(value instanceof Uint8Array);
}

/** A socket that speaks logical frames. */
export class InlineWebSocketClient {
  readonly #socket: InlineSocket;
  readonly #decoder: LogicalFrameDecoder;
  #pending: LogicalFrame[] = [];

  constructor(socket: InlineSocket, role: WsRole) {
    this.#socket = socket;
    this.#decoder = new LogicalFrameDecoder(role);
  }

  /** Send one logical frame. */
  async sendFrame(payload: LogicalFrame): Promise<void> {
    await this.#socket.send(encodeLogicalFrame(payload));
  }

  /**
   * Send a frame, refusing anything that is not one.
   *
   * @throws {TypeError} For a value that is not a mapping — a list or a bare
   *   string has no type to dispatch on, and encoding it would produce a frame
   *   nobody can act on.
   */
  async sendJson(data: unknown): Promise<void> {
    if (!isFrame(data)) {
      throw new TypeError(`expected mapping payload, got ${describe(data)}`);
    }
    await this.sendFrame(data);
  }

  /**
   * Send whatever this is, as whatever it should be.
   *
   * @throws {TypeError} For a string holding a JSON *object*. That is the one
   *   value that reads as a control frame and is not one, so it is refused
   *   rather than delivered as text nobody parses. A JSON list, number or
   *   bare string is not mistakable for a frame and goes through untouched.
   */
  async send(data: unknown): Promise<void> {
    // Stated first, though {@link isFrame} would refuse bytes below and they
    // would reach the same `send` at the end. Either check alone is enough;
    // dropping both is what would encode a binary payload as a frame.
    if (data instanceof Uint8Array) {
      await this.#socket.send(data);
      return;
    }
    if (typeof data === "string") {
      let parsed: unknown;
      try {
        parsed = JSON.parse(data);
      } catch {
        // Not JSON at all, so not mistakable for a frame.
        await this.#socket.send(data);
        return;
      }
      if (isFrame(parsed)) {
        throw new TypeError("bare JSON control strings are not accepted; use sendFrame() or sendJson()");
      }
      await this.#socket.send(data);
      return;
    }
    if (isFrame(data)) {
      await this.sendFrame(data);
      return;
    }
    await this.#socket.send(data as string);
  }

  /**
   * The next logical frame, reading as much as it takes.
   *
   * @throws {TypeError} If the socket hands over something that is not text: a
   *   binary payload cannot carry this protocol, and treating it as text would
   *   corrupt whatever it is.
   */
  async recvFrame(): Promise<LogicalFrame> {
    for (;;) {
      const next = this.#pending.shift();
      if (next !== undefined) {
        return next;
      }
      const raw = await this.#socket.recv();
      if (typeof raw !== "string") {
        throw new TypeError(`expected text WebSocket payload, got ${describe(raw)}`);
      }
      // Assigned rather than appended: this is only reached once the queue has
      // been drained, so there is nothing to append to.
      this.#pending = this.#decoder.feed(raw);
    }
  }
}

/** Name a value's type the way the reference's messages do. */
function describe(value: unknown): string {
  if (value === null) {
    return "NoneType";
  }
  if (Array.isArray(value)) {
    return "list";
  }
  if (value instanceof Uint8Array) {
    return "bytes";
  }
  switch (typeof value) {
    case "string":
      return "str";
    case "number":
      return Number.isInteger(value) ? "int" : "float";
    case "boolean":
      return "bool";
    default:
      return "object";
  }
}
