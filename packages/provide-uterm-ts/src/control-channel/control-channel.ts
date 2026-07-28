//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Inline DLE/STX control framing for mixed terminal data and control
 * messages.
 *
 * Control frame headers store the UTF-8 byte length of the JSON payload, not
 * the character count. ASCII payloads therefore keep their historical wire
 * shape while raw Unicode payloads interoperate with browser runtimes.
 *
 * Port of the Python module `provide.uterm.control_channel` and the Go
 * package `controlchannel`.
 *
 * ## String indexing
 *
 * CPython indexes strings by code point; JavaScript indexes by UTF-16 code
 * unit. Every index in this module is a UTF-16 index, and the payload walker
 * advances two units across a surrogate pair, so the *textual* positions
 * agree with the reference implementation even though the numbers differ.
 * The frame header is pure ASCII, where the two coincide.
 */

/** Data Link Escape — the control-frame introducer. */
export const DLE = "\x10";
/** Start of Text — the second byte of the control-frame magic. */
export const STX = "\x02";

/** DLE STX + 8 hex digits + ':'. */
const HEADER_BYTES = 11;
/** Protocol ceiling on a single control payload. */
const MAX_CONTROL_PAYLOAD_BYTES = 1_048_576;
/** Default ceiling on buffered-but-undecoded input. */
const DEFAULT_BUFFER_BYTES = 10_485_760;
/**
 * Maximum JSON nesting depth in a control frame. The 1 MB frame size limit
 * bounds the *size* of a hostile payload, but a deeply nested structure like
 * `[[[…]]]` of depth ~500 fits in well under 1 MB and would burn stack/CPU on
 * every consumer that walks the decoded object. 32 is well above any
 * legitimate control-frame shape.
 */
const MAX_CONTROL_FRAME_DEPTH = 32;

/** Raised when an inline control frame is malformed. */
export class ControlFrameProtocolError extends Error {
  override readonly name = "ControlFrameProtocolError";
}

/** Decoded terminal data from the inline stream. */
export interface DataChunk {
  readonly kind: "data";
  readonly data: string;
}

/** Decoded control payload from the inline stream. */
export interface ControlChunk {
  readonly kind: "control";
  readonly control: Record<string, unknown>;
}

/** Either kind of event the decoder emits. */
export type ControlFrameChunk = DataChunk | ControlChunk;

/** Report whether `char` is an ASCII hex digit, as `string.hexdigits` does. */
function isHexDigit(char: string): boolean {
  return /^[0-9a-fA-F]$/.test(char);
}

/** UTF-8 encoded length of a single code point. */
function utf8Length(codePoint: number): number {
  if (codePoint < 0x80) {
    return 1;
  }
  if (codePoint < 0x800) {
    return 2;
  }
  if (codePoint < 0x10000) {
    return 3;
  }
  return 4;
}

/**
 * Locate the index ending a payload of `payloadBytes` UTF-8 bytes.
 *
 * Returns `undefined` when `buf` does not yet hold that many bytes from
 * `start` — appending more input can still complete it.
 *
 * @throws {ControlFrameProtocolError} When the declared byte length splits a
 *   code point, which appending more text can never fix.
 */
function utf8PayloadEnd(buf: string, start: number, payloadBytes: number): number | undefined {
  let byteCount = 0;
  let idx = start;
  while (idx < buf.length && byteCount < payloadBytes) {
    const codePoint = buf.codePointAt(idx) as number;
    byteCount += utf8Length(codePoint);
    idx += codePoint > 0xffff ? 2 : 1;
    if (byteCount > payloadBytes) {
      throw new ControlFrameProtocolError("invalid control payload length");
    }
  }
  return byteCount < payloadBytes ? undefined : idx;
}

/** Encode terminal data for the inline stream. */
export function encodeTerminalData(data: string): string {
  return data.replaceAll(DLE, DLE + DLE);
}

/**
 * Encode a control payload for the inline stream.
 *
 * The serialisation is compact — no whitespace between tokens — and
 * non-ASCII characters are emitted literally, matching CPython's
 * `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`.
 */
export function encodeControlFrame(payload: Record<string, unknown>): string {
  const serialized = JSON.stringify(payload);
  const length = Buffer.byteLength(serialized, "utf-8").toString(16).padStart(8, "0");
  return `${DLE}${STX}${length}:${serialized}`;
}

/**
 * Report whether `message` is exactly one complete control-framed payload.
 *
 * The check is structural only: it validates the magic bytes, the length
 * header syntax, and that the declared UTF-8 payload bytes are all present.
 * It does not parse the payload as JSON.
 */
export function isControlFrame(message: string): boolean {
  if (message.length < HEADER_BYTES) {
    return false;
  }
  if (!message.startsWith(DLE + STX)) {
    return false;
  }
  if (message[10] !== ":") {
    return false;
  }
  const lengthHex = message.slice(2, 10);
  for (const char of lengthHex) {
    if (!isHexDigit(char)) {
      return false;
    }
  }
  const payloadBytes = Number.parseInt(lengthHex, 16);
  // Reject a non-canonical spelling such as an uppercase digit.
  if (payloadBytes.toString(16).padStart(8, "0") !== lengthHex) {
    return false;
  }
  if (payloadBytes > MAX_CONTROL_PAYLOAD_BYTES) {
    return false;
  }
  let payloadEnd: number | undefined;
  try {
    payloadEnd = utf8PayloadEnd(message, HEADER_BYTES, payloadBytes);
  } catch {
    return false;
  }
  return payloadEnd !== undefined && payloadEnd === message.length;
}

/**
 * Reject `value` when it nests deeper than `maxDepth`.
 *
 * Walks the decoded structure iteratively so a pathological payload cannot
 * blow the stack inside the check itself. Primitive leaves count as depth 0;
 * each object or array adds one.
 */
function checkJsonDepth(value: Record<string, unknown>, maxDepth: number): void {
  const stack: Array<{ node: object; depth: number }> = [{ node: value, depth: 1 }];
  for (;;) {
    const entry = stack.pop();
    if (entry === undefined) {
      return;
    }
    const { node, depth } = entry;
    if (depth > maxDepth) {
      throw new ControlFrameProtocolError(`control payload nests deeper than ${maxDepth}`);
    }
    // Only containers are ever pushed, so the popped node needs no re-check;
    // primitives and nulls are filtered here on the way in.
    for (const child of Array.isArray(node) ? node : Object.values(node)) {
      if (typeof child === "object" && child !== null) {
        stack.push({ node: child, depth: depth + 1 });
      }
    }
  }
}

/** Tunables and hooks for {@link ControlFrameDecoder}. */
export interface ControlFrameDecoderOptions {
  /** Ceiling on a single payload, clamped to at least 1. */
  maxControlPayloadBytes?: number;
  /** Ceiling on buffered-but-undecoded input, clamped to at least 1. */
  maxBufferBytes?: number;
  /** Maximum JSON nesting depth, clamped to at least 1. */
  maxFrameDepth?: number;
  /** Notified once per protocol violation, before the error is thrown. */
  onError?: (code: string) => void;
}

/** Incrementally decode the inline DLE/STX control-frame stream. */
export class ControlFrameDecoder {
  readonly #maxControlPayloadBytes: number;
  readonly #maxBufferBytes: number;
  readonly #maxFrameDepth: number;
  readonly #onError: ((code: string) => void) | undefined;
  #buffer = "";

  constructor(options: ControlFrameDecoderOptions = {}) {
    this.#maxControlPayloadBytes = Math.max(1, Math.trunc(options.maxControlPayloadBytes ?? MAX_CONTROL_PAYLOAD_BYTES));
    this.#maxBufferBytes = Math.max(1, Math.trunc(options.maxBufferBytes ?? DEFAULT_BUFFER_BYTES));
    this.#maxFrameDepth = Math.max(1, Math.trunc(options.maxFrameDepth ?? MAX_CONTROL_FRAME_DEPTH));
    this.#onError = options.onError;
  }

  /** Fire the error hook and build the error to throw. */
  #reportError(message: string): ControlFrameProtocolError {
    this.#onError?.("control_frame_protocol_error");
    return new ControlFrameProtocolError(message);
  }

  /** Drop all buffered input, so a rejection cannot poison later feeds. */
  #reset(): void {
    this.#buffer = "";
  }

  /** Decode all complete events from `chunk` and buffer the rest. */
  feed(chunk: string): ControlFrameChunk[] {
    if (typeof chunk !== "string") {
      throw new TypeError(`control frame chunks must be str, got ${typeof chunk}`);
    }
    const candidate = this.#buffer + chunk;
    const total = Buffer.byteLength(candidate, "utf-8");
    if (total > this.#maxBufferBytes) {
      this.#reset();
      throw this.#reportError(`control frame buffer overflow: ${total} > ${this.#maxBufferBytes}`);
    }
    this.#buffer = candidate;
    try {
      return this.#drain(false);
    } catch (error) {
      this.#reset();
      throw error;
    }
  }

  /** Decode any remaining buffered data and reject truncated frames. */
  finish(): ControlFrameChunk[] {
    let events: ControlFrameChunk[];
    try {
      events = this.#drain(true);
    } catch (error) {
      this.#reset();
      throw error;
    }
    // Defensive guard carried over from the reference implementation. A
    // final drain either consumes the whole buffer or throws, so there is no
    // input that reaches here with residue; it stays as a backstop against a
    // future change to the scan loop.
    /* v8 ignore next 4 */
    if (this.#buffer !== "") {
      this.#reset();
      throw this.#reportError("truncated control frame");
    }
    return events;
  }

  /** Parse and validate a control frame JSON payload. */
  #parseFramePayload(raw: string): Record<string, unknown> {
    let payload: unknown;
    try {
      payload = JSON.parse(raw);
    } catch {
      throw this.#reportError("invalid control json");
    }
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
      throw this.#reportError("control payload must be an object");
    }
    const record = payload as Record<string, unknown>;
    try {
      checkJsonDepth(record, this.#maxFrameDepth);
    } catch (error) {
      throw this.#reportError((error as Error).message);
    }
    return record;
  }

  /**
   * Parse a control frame starting at `idx`.
   *
   * Returns `undefined` when the frame is not yet complete, which is only
   * valid while more input may still arrive.
   */
  #tryParseFrame(buf: string, idx: number, final: boolean): { chunk: ControlChunk; end: number } | undefined {
    if (buf.length - idx < HEADER_BYTES) {
      if (final) {
        throw this.#reportError("truncated control frame");
      }
      return undefined;
    }
    const lengthHex = buf.slice(idx + 2, idx + 10);
    const separator = buf[idx + 10];
    if (separator !== ":" || ![...lengthHex].every(isHexDigit)) {
      throw this.#reportError("invalid control header");
    }
    const payloadBytes = Number.parseInt(lengthHex, 16);
    if (payloadBytes > MAX_CONTROL_PAYLOAD_BYTES || payloadBytes > this.#maxControlPayloadBytes) {
      throw this.#reportError("control payload too large");
    }
    const payloadStart = idx + HEADER_BYTES;
    let end: number | undefined;
    try {
      end = utf8PayloadEnd(buf, payloadStart, payloadBytes);
    } catch (error) {
      throw this.#reportError((error as Error).message);
    }
    if (end === undefined) {
      if (final) {
        throw this.#reportError("truncated control frame");
      }
      return undefined;
    }
    return { chunk: { kind: "control", control: this.#parseFramePayload(buf.slice(payloadStart, end)) }, end };
  }

  /** Scan the buffer, emitting every complete event and keeping the tail. */
  #drain(final: boolean): ControlFrameChunk[] {
    const events: ControlFrameChunk[] = [];
    const buf = this.#buffer;
    const bufLength = buf.length;
    let idx = 0;
    // Accumulated plain data: literal slices interleaved with unescaped DLEs.
    let dataParts: string[] = [];
    let dataStart = 0;

    /** Emit everything accumulated so far as a single data chunk. */
    const emitData = (): void => {
      if (dataStart < idx) {
        dataParts.push(buf.slice(dataStart, idx));
      }
      if (dataParts.length > 0) {
        events.push({ kind: "data", data: dataParts.join("") });
        dataParts = [];
      }
      dataStart = idx;
    };

    while (idx < bufLength) {
      if (buf[idx] !== DLE) {
        idx += 1;
        continue;
      }
      if (idx + 1 >= bufLength) {
        if (final) {
          throw this.#reportError("truncated control frame");
        }
        break;
      }
      const next = buf[idx + 1];
      if (next === DLE) {
        if (dataStart < idx) {
          dataParts.push(buf.slice(dataStart, idx));
        }
        dataParts.push(DLE);
        idx += 2;
        dataStart = idx;
        continue;
      }
      if (next !== STX) {
        throw this.#reportError("invalid control prefix");
      }
      emitData();
      const parsed = this.#tryParseFrame(buf, idx, final);
      if (parsed === undefined) {
        break;
      }
      idx = parsed.end;
      dataStart = idx;
      events.push(parsed.chunk);
    }

    this.#buffer = buf.slice(idx);
    if (dataStart < idx) {
      dataParts.push(buf.slice(dataStart, idx));
    }
    if (dataParts.length > 0) {
      events.push({ kind: "data", data: dataParts.join("") });
    }
    return events;
  }
}
