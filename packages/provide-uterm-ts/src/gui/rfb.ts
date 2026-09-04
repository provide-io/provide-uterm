// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * An RFB (VNC) client, so `gui/attach` reaches a real screen from this port too.
 *
 * Ported from the C# canonical (`Vnc/RfbClient.cs`) and the Python reference
 * (`server/rfb_session.py`), which arrived first and which this is held to.
 *
 * The port coverage here is deliberate rather than accidental: Go wires memory
 * and litevirt and refuses rfb, "a documented gap mirroring C#'s 501 for
 * litevirt" in its own words. C# and Python wire memory and rfb. This joins
 * them, which leaves litevirt as the protocol no implementation but Go speaks —
 * and that is what the shared corpus records as its unsupported case.
 *
 * The shape differs from the reference in exactly one way, and it is a runtime
 * difference rather than a behavioural one: Python holds the framebuffer with a
 * daemon thread doing blocking reads, and Node has no threads, so the same loop
 * is written against an async byte reader fed by socket events. What goes on
 * the wire, what is refused, and what a screenshot returns are identical.
 */

import { connect as netConnect, type Socket } from "node:net";

import { type GraphicalSession, MAX_DIMENSION, RgbaImage } from "./session.js";

/** Encodings this client asks for, in preference order. */
export const ENCODING_RAW = 0;
export const ENCODING_COPY_RECT = 1;

/** The only security type supported, matching both references. */
export const SECURITY_NONE = 1;

const MSG_FRAMEBUFFER_UPDATE = 0;

/** Refuse absurd values from a hostile or broken server before allocating. */
const MAX_RECTS = 4096;
const MAX_DESKTOP_NAME = 4096;

/** A duplex byte stream. `net.Socket` is one; so is a test double. */
export interface RfbStream {
  on(event: "data", listener: (chunk: Buffer) => void): unknown;
  on(event: "end" | "close", listener: () => void): unknown;
  on(event: "error", listener: (err: Error) => void): unknown;
  write(chunk: Uint8Array): unknown;
  destroy(): unknown;
}

/**
 * Turns a chunked stream into `await read(n)`.
 *
 * The handshake is a strict sequence of fixed-width reads, which is trivial
 * when reads block and needs this when they do not.
 */
export class ByteReader {
  #buffered: Buffer = Buffer.alloc(0);
  #want: { count: number; resolve: (b: Buffer) => void; reject: (e: Error) => void } | null = null;
  #ended: Error | null = null;

  constructor(stream: RfbStream) {
    stream.on("data", (chunk: Buffer) => {
      this.#buffered = Buffer.concat([this.#buffered, chunk]);
      this.#pump();
    });
    stream.on("end", () => this.#fail(new Error("RFB stream ended")));
    stream.on("error", (err: Error) => this.#fail(err));
  }

  /** Resolve a pending read as soon as enough bytes have arrived. */
  #pump(): void {
    const want = this.#want;
    if (want === null || this.#buffered.length < want.count) {
      return;
    }
    const taken = this.#buffered.subarray(0, want.count);
    this.#buffered = this.#buffered.subarray(want.count);
    this.#want = null;
    want.resolve(taken);
  }

  #fail(error: Error): void {
    this.#ended = error;
    const want = this.#want;
    if (want !== null) {
      this.#want = null;
      want.reject(error);
    }
  }

  /** Exactly `count` bytes, or a rejection. Short reads are protocol errors. */
  read(count: number): Promise<Buffer> {
    if (this.#buffered.length >= count) {
      const taken = this.#buffered.subarray(0, count);
      this.#buffered = this.#buffered.subarray(count);
      return Promise.resolve(taken);
    }
    if (this.#ended !== null) {
      return Promise.reject(this.#ended);
    }
    return new Promise<Buffer>((resolve, reject) => {
      this.#want = { count, resolve, reject };
    });
  }
}

/** Agree a ProtocolVersion, preferring 3.8 when the server offers it. */
export async function negotiateVersion(reader: ByteReader, stream: RfbStream): Promise<string> {
  const serverVersion = (await reader.read(12)).toString("ascii");
  if (!serverVersion.startsWith("RFB ")) {
    throw new Error(`not an RFB server: ${JSON.stringify(serverVersion)}`);
  }
  const clientVersion = serverVersion.includes("003.008") ? "RFB 003.008\n" : serverVersion;
  stream.write(Buffer.from(clientVersion, "ascii"));
  return clientVersion;
}

/** Complete the security handshake, which must land on type `None`. */
export async function negotiateSecurity(reader: ByteReader, stream: RfbStream, clientVersion: string): Promise<void> {
  if (clientVersion.includes("003.007") || clientVersion.includes("003.008")) {
    const count = (await reader.read(1))[0] as number;
    if (count === 0) {
      throw new Error("RFB security handshake failed (server offered no types)");
    }
    const offered = await reader.read(count);
    if (!offered.includes(SECURITY_NONE)) {
      throw new Error(
        `RFB server does not offer security type None (offered ${[...offered].sort((a, b) => a - b).join(", ")})`,
      );
    }
    stream.write(Buffer.from([SECURITY_NONE]));
    // SecurityResult is 3.8 only; consuming it on 3.7 would desynchronise.
    if (clientVersion.includes("003.008")) {
      const result = (await reader.read(4)).readUInt32BE(0);
      if (result !== 0) {
        throw new Error("RFB security rejected");
      }
    }
    return;
  }

  const securityType = (await reader.read(4)).readUInt32BE(0);
  if (securityType !== SECURITY_NONE) {
    throw new Error(`unsupported RFB security type ${securityType}`);
  }
}

/** Read ServerInit and return validated `[width, height]`. */
export async function readServerInit(reader: ByteReader): Promise<[number, number]> {
  const header = await reader.read(24);
  const width = header.readUInt16BE(0);
  const height = header.readUInt16BE(2);
  if (width === 0 || height === 0 || width > MAX_DIMENSION || height > MAX_DIMENSION) {
    throw new Error(`RFB framebuffer dimensions out of range: ${width}x${height}`);
  }
  const nameLength = header.readUInt32BE(20);
  if (nameLength > MAX_DESKTOP_NAME) {
    throw new Error("RFB desktop name too long");
  }
  if (nameLength > 0) {
    await reader.read(nameLength);
  }
  return [width, height];
}

/** SetPixelFormat: 32bpp true colour, BGRA, which the blit assumes. */
export function encodeSetPixelFormat(): Buffer {
  const message = Buffer.alloc(20);
  message.writeUInt8(0, 0);
  message.writeUInt8(32, 4); // bits-per-pixel
  message.writeUInt8(24, 5); // depth
  message.writeUInt8(0, 6); // big-endian-flag
  message.writeUInt8(1, 7); // true-colour-flag
  message.writeUInt16BE(255, 8);
  message.writeUInt16BE(255, 10);
  message.writeUInt16BE(255, 12);
  message.writeUInt8(16, 14); // red-shift
  message.writeUInt8(8, 15); // green-shift
  message.writeUInt8(0, 16); // blue-shift
  return message;
}

export function encodeSetEncodings(): Buffer {
  const message = Buffer.alloc(12);
  message.writeUInt8(2, 0);
  message.writeUInt16BE(2, 2);
  message.writeInt32BE(ENCODING_RAW, 4);
  message.writeInt32BE(ENCODING_COPY_RECT, 8);
  return message;
}

export function encodeUpdateRequest(width: number, height: number, incremental: boolean): Buffer {
  const message = Buffer.alloc(10);
  message.writeUInt8(3, 0);
  message.writeUInt8(incremental ? 1 : 0, 1);
  message.writeUInt16BE(width, 6);
  message.writeUInt16BE(height, 8);
  return message;
}

/** PointerEvent (message type 5). */
export function encodePointerEvent(x: number, y: number, buttonMask: number): Buffer {
  const message = Buffer.alloc(6);
  message.writeUInt8(5, 0);
  message.writeUInt8(buttonMask & 0xff, 1);
  message.writeUInt16BE(Math.max(0, x), 2);
  message.writeUInt16BE(Math.max(0, y), 4);
  return message;
}

/** KeyEvent (message type 4). */
export function encodeKeyEvent(keySym: number, down: boolean): Buffer {
  const message = Buffer.alloc(8);
  message.writeUInt8(4, 0);
  message.writeUInt8(down ? 1 : 0, 1);
  message.writeUInt32BE(keySym >>> 0, 4);
  return message;
}

/** A live RFB connection presented as a `GraphicalSession`. */
export class RfbGraphicalSession implements GraphicalSession {
  readonly width: number;
  readonly height: number;
  readonly #stream: RfbStream;
  readonly #reader: ByteReader;
  readonly #framebuffer: RgbaImage;
  #closed = false;
  /** Resolves when the update loop stops. Tests await it; nothing else needs it. */
  readonly settled: Promise<void>;

  constructor(stream: RfbStream, reader: ByteReader, width: number, height: number) {
    this.#stream = stream;
    this.#reader = reader;
    this.width = width;
    this.height = height;
    this.#framebuffer = new RgbaImage(width, height);
    this.settled = this.#readLoop();
  }

  /** Dial `host:port`, complete the handshake, and start tracking. */
  static async connect(
    host: string,
    port: number,
    options: { dial?: (host: string, port: number) => RfbStream } = {},
  ): Promise<RfbGraphicalSession> {
    const dial = options.dial ?? ((h, p) => netConnect({ host: h, port: p }) as unknown as Socket as RfbStream);
    const stream = dial(host, port);
    const reader = new ByteReader(stream);
    try {
      const clientVersion = await negotiateVersion(reader, stream);
      await negotiateSecurity(reader, stream, clientVersion);
      stream.write(Buffer.from([1])); // ClientInit, shared = 1
      const [width, height] = await readServerInit(reader);
      stream.write(encodeSetPixelFormat());
      stream.write(encodeSetEncodings());
      stream.write(encodeUpdateRequest(width, height, false));
      return new RfbGraphicalSession(stream, reader, width, height);
    } catch (error) {
      stream.destroy();
      throw error;
    }
  }

  /** Apply framebuffer updates until the peer closes or we do. */
  async #readLoop(): Promise<void> {
    try {
      while (!this.#closed) {
        const messageType = (await this.#reader.read(1))[0] as number;
        if (messageType !== MSG_FRAMEBUFFER_UPDATE) {
          // Bell and ServerCutText carry no length we can skip blindly, so
          // stopping beats guessing and desynchronising the stream.
          return;
        }
        const rectCount = (await this.#reader.read(3)).readUInt16BE(1);
        if (rectCount > MAX_RECTS) {
          throw new Error(`RFB rectangle count too large: ${rectCount}`);
        }
        for (let index = 0; index < rectCount; index += 1) {
          await this.#applyRect();
        }
        if (!this.#closed) {
          this.#stream.write(encodeUpdateRequest(this.width, this.height, true));
        }
      }
    } catch {
      // Teardown and peer loss look the same here; both end the loop.
    } finally {
      this.#closed = true;
    }
  }

  async #applyRect(): Promise<void> {
    const header = await this.#reader.read(12);
    const x = header.readUInt16BE(0);
    const y = header.readUInt16BE(2);
    const w = header.readUInt16BE(4);
    const h = header.readUInt16BE(6);
    const encoding = header.readInt32BE(8);
    if (w === 0 || h === 0) {
      return;
    }
    if (x + w > this.width || y + h > this.height) {
      throw new Error(`RFB rect out of bounds: ${x},${y} ${w}x${h}`);
    }
    if (encoding === ENCODING_RAW) {
      this.#blit(x, y, w, h, await this.#reader.read(w * h * 4));
      return;
    }
    if (encoding === ENCODING_COPY_RECT) {
      // Consume the source coordinates; a missed copyrect costs staleness in a
      // region, not a desynchronised stream.
      await this.#reader.read(4);
      return;
    }
    throw new Error(`RFB encoding not negotiated: ${encoding}`);
  }

  /** Copy a BGRA rectangle into the RGBA framebuffer. */
  #blit(x: number, y: number, w: number, h: number, pixels: Buffer): void {
    const target = this.#framebuffer.pixels;
    const stride = this.width * 4;
    for (let row = 0; row < h; row += 1) {
      const source = row * w * 4;
      const destination = (y + row) * stride + x * 4;
      for (let column = 0; column < w; column += 1) {
        const s = source + column * 4;
        const d = destination + column * 4;
        target[d] = pixels[s + 2] as number;
        target[d + 1] = pixels[s + 1] as number;
        target[d + 2] = pixels[s] as number;
        target[d + 3] = 255;
      }
    }
  }

  screenshot(): RgbaImage {
    return new RgbaImage(this.width, this.height, this.#framebuffer.pixels);
  }

  injectPointer(x: number, y: number, buttonMask: number): void {
    this.#send(encodePointerEvent(x, y, buttonMask));
  }

  injectKey(keySym: number, down: boolean): void {
    this.#send(encodeKeyEvent(keySym, down));
  }

  #send(payload: Buffer): void {
    if (this.#closed) {
      throw new Error("RFB session is closed");
    }
    this.#stream.write(payload);
  }

  /** Stop the loop and drop the socket. */
  close(): void {
    this.#closed = true;
    this.#stream.destroy();
  }
}
