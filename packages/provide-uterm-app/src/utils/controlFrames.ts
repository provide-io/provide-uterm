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
}

export function encodeControlFrame(payload: Record<string, unknown>): string {
  const json = JSON.stringify(payload);
  return `${DLE}${STX}${utf8ByteLength(json).toString(16).padStart(8, "0")}:${json}`;
}

export class ControlFrameDecoder {
  private buffer = "";
  private readonly maxPayloadBytes: number;

  constructor(options: ControlFrameDecoderOptions = {}) {
    this.maxPayloadBytes = options.maxPayloadBytes ?? 1024 * 1024;
  }

  reset(): void {
    this.buffer = "";
  }

  feed(chunk: string): Array<Record<string, unknown>> {
    this.buffer += String(chunk ?? "");
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
        cursor = dleIdx + 1;
        continue;
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
