//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The tunnel wire format.
 *
 * Port of `provide.uterm.tunnel.protocol`. A frame is two header bytes — the
 * channel and the flags — followed by the payload, which is why a frame
 * shorter than two bytes is not a frame with an empty payload but no frame at
 * all.
 *
 * Control messages ride channel zero as compact JSON and must name a type,
 * since the type is what the far end dispatches on: a message without one
 * would be delivered nowhere and reported as nothing.
 */

import { pyJsonDumps } from "../pycompat/json.ts";

/** Control messages: what the two ends say to each other. */
export const CHANNEL_CONTROL = 0x00;
/** Terminal bytes. */
export const CHANNEL_DATA = 0x01;
/** A forwarded TCP stream. */
export const CHANNEL_TCP = 0x02;
/** A forwarded HTTP exchange. */
export const CHANNEL_HTTP = 0x03;

/** An ordinary frame. */
export const FLAG_DATA = 0x00;
/** The last frame on its channel. */
export const FLAG_EOF = 0x01;

/** A frame that is malformed, or arguments that cannot make one. */
export class TunnelProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TunnelProtocolError";
  }
}

/** A decoded frame. */
export interface TunnelFrame {
  channel: number;
  flags: number;
  payload: Uint8Array;
}

/** Whether this frame ends its channel. */
export function isEof(frame: TunnelFrame): boolean {
  return (frame.flags & FLAG_EOF) !== 0;
}

/** Whether this frame is one of the two ends talking, rather than traffic. */
export function isControl(frame: TunnelFrame): boolean {
  return frame.channel === CHANNEL_CONTROL;
}

/** Whether a value fits in the single byte the header gives it. */
function fitsInByte(value: number): boolean {
  return Number.isInteger(value) && value >= 0 && value <= 0xff;
}

/**
 * Encode a frame: the channel, the flags, then the payload.
 *
 * @throws {TunnelProtocolError} If either header value does not fit in a byte.
 */
export function encodeFrame(channel: number, payload: Uint8Array, flags: number = FLAG_DATA): Uint8Array {
  if (!fitsInByte(channel)) {
    throw new TunnelProtocolError("channel must be 0..255");
  }
  if (!fitsInByte(flags)) {
    throw new TunnelProtocolError("flags must be 0..255");
  }
  const frame = new Uint8Array(2 + payload.length);
  frame[0] = channel;
  frame[1] = flags;
  frame.set(payload, 2);
  return frame;
}

/**
 * Decode a frame.
 *
 * @throws {TunnelProtocolError} If there is not even a header.
 */
export function decodeFrame(data: Uint8Array): TunnelFrame {
  if (data.length < 2) {
    throw new TunnelProtocolError("frame too short");
  }
  return { channel: data[0] as number, flags: data[1] as number, payload: data.slice(2) };
}

/**
 * Encode a control message as a frame on channel zero.
 *
 * @throws {TunnelProtocolError} If the message names no type. A message the
 *   far end cannot dispatch is refused here rather than delivered nowhere.
 */
export function encodeControl(message: Record<string, unknown>): Uint8Array {
  if (!Object.hasOwn(message, "type")) {
    throw new TunnelProtocolError("control message must have a 'type' key");
  }
  // The reference's `json.dumps` with compact separators: keys sorted and
  // anything outside ASCII escaped, so both ends produce the same bytes.
  return encodeFrame(CHANNEL_CONTROL, new TextEncoder().encode(pyJsonDumps(message, { sortKeys: false })));
}

/**
 * Decode a control payload.
 *
 * @throws {TunnelProtocolError} If it is not readable JSON, or is JSON that is
 *   not an object — a list or a bare string has no type to dispatch on.
 */
export function decodeControl(payload: Uint8Array): Record<string, unknown> {
  let decoded: unknown;
  try {
    decoded = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(payload));
  } catch {
    throw new TunnelProtocolError("invalid control payload");
  }
  if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) {
    throw new TunnelProtocolError("control payload must be a JSON object");
  }
  return decoded as Record<string, unknown>;
}
