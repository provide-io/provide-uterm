//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Decode inline control frames from the terminal WebSocket stream.
 *
 * The wire format uses DLE (0x10) STX (0x02) framing:
 *   DLE STX [8-hex-digit UTF-8 byte length] : [json-payload]
 */

const DLE = "\x10";
const STX = "\x02";
const CONTROL_LEN_RE = /^[0-9a-fA-F]{8}$/;
const TEXT_ENCODER = new TextEncoder();
const DEFAULT_MAX_PAYLOAD_BYTES = 1024 * 1024;
const DEFAULT_MAX_BUFFER_BYTES = 10 * 1024 * 1024;
const DEFAULT_MAX_JSON_DEPTH = 32;

function utf8ByteLength(value: string): number {
  return TEXT_ENCODER.encode(value).byteLength;
}

function utf8PayloadEnd(raw: string, start: number, payloadBytes: number): number | null {
  let byteCount = 0;
  let cursor = start;
  while (cursor < raw.length && byteCount < payloadBytes) {
    const codePoint = raw.codePointAt(cursor);
    if (codePoint === undefined) break;
    const char = String.fromCodePoint(codePoint);
    byteCount += utf8ByteLength(char);
    cursor += char.length;
    if (byteCount > payloadBytes) {
      throw new Error("invalid control payload length");
    }
  }
  return byteCount === payloadBytes ? cursor : null;
}

interface ControlFrameDecoderOptions {
  maxPayloadBytes?: number;
  maxBufferBytes?: number;
  maxJsonDepth?: number;
}

function checkJsonDepth(value: unknown, maxDepth: number): void {
  const stack: Array<{ node: unknown; depth: number }> = [{ node: value, depth: 1 }];
  while (stack.length > 0) {
    const entry = stack.pop() as { node: unknown; depth: number };
    if (entry.depth > maxDepth) {
      throw new Error(`control payload nests deeper than ${maxDepth}`);
    }
    if (typeof entry.node !== "object" || entry.node === null) continue;
    const children = Array.isArray(entry.node) ? entry.node : Object.values(entry.node);
    for (const child of children) {
      if (typeof child === "object" && child !== null) {
        stack.push({ node: child, depth: entry.depth + 1 });
      }
    }
  }
}

export function encodeControlFrame(payload: Record<string, unknown>): string {
  const json = JSON.stringify(payload);
  return `${DLE}${STX}${utf8ByteLength(json).toString(16).padStart(8, "0")}:${json}`;
}

export class ControlFrameDecoder {
  private buffer = "";
  private readonly maxPayloadBytes: number;
  private readonly maxBufferBytes: number;
  private readonly maxJsonDepth: number;

  constructor(options: ControlFrameDecoderOptions = {}) {
    this.maxPayloadBytes = options.maxPayloadBytes ?? DEFAULT_MAX_PAYLOAD_BYTES;
    this.maxBufferBytes = options.maxBufferBytes ?? DEFAULT_MAX_BUFFER_BYTES;
    this.maxJsonDepth = options.maxJsonDepth ?? DEFAULT_MAX_JSON_DEPTH;
  }

  reset(): void {
    this.buffer = "";
  }

  feed(chunk: string): Array<Record<string, unknown>> {
    try {
      return this.feedUnsafe(chunk);
    } catch (err) {
      this.reset();
      throw err;
    }
  }

  private feedUnsafe(chunk: string): Array<Record<string, unknown>> {
    this.buffer += String(chunk ?? "");
    if (utf8ByteLength(this.buffer) > this.maxBufferBytes) {
      throw new Error("control frame buffer overflow");
    }
    const frames: Array<Record<string, unknown>> = [];
    let cursor = 0;

    while (cursor < this.buffer.length) {
      const dleIdx = this.buffer.indexOf(DLE, cursor);
      if (dleIdx === -1) {
        this.buffer = "";
        return frames;
      }

      if (dleIdx + 1 >= this.buffer.length) {
        this.buffer = this.buffer.slice(dleIdx);
        return frames;
      }

      const marker = this.buffer[dleIdx + 1];
      if (marker === DLE) {
        cursor = dleIdx + 2;
        continue;
      }
      if (marker !== STX) {
        throw new Error("invalid control frame prefix");
      }

      if (dleIdx + 11 > this.buffer.length) {
        this.buffer = this.buffer.slice(dleIdx);
        return frames;
      }

      const header = this.buffer.slice(dleIdx + 2, dleIdx + 10);
      if (!CONTROL_LEN_RE.test(header)) {
        throw new Error("invalid control frame length");
      }
      if (this.buffer[dleIdx + 10] !== ":") {
        throw new Error("invalid control frame separator");
      }

      const payloadLength = Number.parseInt(header, 16);
      if (!Number.isFinite(payloadLength) || payloadLength > this.maxPayloadBytes) {
        throw new Error("control payload too large");
      }

      const payloadStart = dleIdx + 11;
      const payloadEnd = utf8PayloadEnd(this.buffer, payloadStart, payloadLength);
      if (payloadEnd === null) {
        this.buffer = this.buffer.slice(dleIdx);
        return frames;
      }

      let parsed: unknown;
      try {
        parsed = JSON.parse(this.buffer.slice(payloadStart, payloadEnd));
      } catch {
        throw new Error("invalid control payload");
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("control payload must be an object");
      }
      checkJsonDepth(parsed, this.maxJsonDepth);

      frames.push(parsed as Record<string, unknown>);
      cursor = payloadEnd;
    }

    this.buffer = "";
    return frames;
  }
}

export function decodeControlFrames(
  raw: string,
  options?: ControlFrameDecoderOptions,
): Array<Record<string, unknown>> {
  return new ControlFrameDecoder(options).feed(raw);
}
