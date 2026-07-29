//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a viewer is allowed to send a graphical session.
 *
 * Port of `provide.uterm.vnc.rfb_filter`, which is itself parity with the Go
 * port's `filterRFBInput`.
 *
 * Everything that only *reads* the screen passes through. The three messages
 * that act on it — keystrokes, pointer movement, clipboard writes — are gated
 * on a permission check. This is the boundary that stops somebody watching a
 * session from typing into it.
 */

/** The client messages this filter understands. */
const SET_PIXEL_FORMAT = 0;
const SET_ENCODINGS = 2;
const FRAMEBUFFER_UPDATE_REQUEST = 3;
const KEY_EVENT = 4;
const POINTER_EVENT = 5;
const CLIENT_CUT_TEXT = 6;

/** How much clipboard a viewer may paste at once. */
export const MAX_CUT_TEXT = 1 << 20;

/** The only security type the relay implements. */
const SECURITY_NONE = 1;

/** How many bytes each fixed-size message carries after its type byte. */
const PIXEL_FORMAT_BYTES = 19;
const UPDATE_REQUEST_BYTES = 9;
const KEY_EVENT_BYTES = 7;
const POINTER_EVENT_BYTES = 5;
const CUT_TEXT_HEADER_BYTES = 7;
const ENCODINGS_HEADER_BYTES = 3;

/** How much is drained at a time when discarding an oversized message. */
const DRAIN_CHUNK = 65_536;

/** RFB's extended clipboard sets the top bit; the rest is the real length. */
const EXTENDED_CLIPBOARD_MASK = 0x7fffffff;

/** A stream ended in the middle of a message. */
export class RfbShortReadError extends Error {}

/** A stream said something this filter does not implement. */
export class RfbProtocolError extends Error {}

/** Whether this viewer may act on the session, rather than only watch it. */
export type CanInject = (sessionId: string, leaseId: string, principalId: string, principalRole: string) => boolean;

/** Where the client's bytes come from. */
export interface ByteSource {
  /** Up to `size` bytes, or fewer at the end of the stream. */
  read(size: number): Uint8Array;
}

/** Where the filtered bytes go. */
export interface ByteSink {
  write(data: Uint8Array): void;
}

/** Who is sending, and what to do about it. */
export interface RfbFilterOptions {
  /** Omitted means nobody may inject — see {@link filterRfbClientInput}. */
  canInject?: CanInject | undefined;
  sessionId: string;
  leaseId: string;
  principalId: string;
  principalRole: string;
  /** Called once the client's first update request has gone upstream. */
  onClientReady?: (() => void) | undefined;
}

/** Read exactly `size` bytes, or fail. */
function readExact(src: ByteSource, size: number): Uint8Array {
  const buffer = src.read(size);
  if (buffer.length < size) {
    throw new RfbShortReadError(`short read: want ${size}, got ${buffer.length}`);
  }
  return buffer;
}

/** Join byte runs into one write, so a message reaches the sink whole. */
function joined(...parts: readonly Uint8Array[]): Uint8Array {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

/**
 * Whether an injecting message may be forwarded.
 *
 * No callback means no: a relay wired up without one would otherwise forward
 * keystrokes from anybody, and failing closed is the only safe reading of
 * "nobody said who may send them".
 */
function allowed(options: RfbFilterOptions): boolean {
  return (
    options.canInject !== undefined &&
    options.canInject(options.sessionId, options.leaseId, options.principalId, options.principalRole)
  );
}

/**
 * Copy a client's RFB messages, gating the ones that act on the session.
 *
 * A refused message is dropped rather than refused: the stream is a byte
 * protocol with no room for an error, so a keystroke a viewer may not send is
 * simply not forwarded — the session stays up and the viewer sees nothing
 * happen.
 *
 * Each message is written in one call, so a concurrent writer of the same sink
 * — the driver injecting periodic update requests — cannot interleave halfway
 * through one.
 *
 * @throws {RfbProtocolError} On a security type or message type this does not
 *   implement.
 * @throws {RfbShortReadError} When the stream ends mid-message.
 */
export function filterRfbClientInput(dst: ByteSink, src: ByteSource, options: RfbFilterOptions): void {
  // The handshake passes through as it is: a version, a security type, and
  // the client's shared-desktop flag.
  dst.write(readExact(src, 12));

  const security = readExact(src, 1);
  if (security[0] !== SECURITY_NONE) {
    throw new RfbProtocolError(`unsupported security type ${security[0]}`);
  }
  dst.write(security);
  dst.write(readExact(src, 1));

  let clientReadyFired = false;
  for (;;) {
    const header = src.read(1);
    if (header.length < 1) {
      // The client hung up between messages, which is how a session ends.
      return;
    }

    switch (header[0]) {
      case SET_PIXEL_FORMAT:
        dst.write(joined(header, readExact(src, PIXEL_FORMAT_BYTES)));
        break;

      case SET_ENCODINGS: {
        const encodings = readExact(src, ENCODINGS_HEADER_BYTES);
        const count = (encodings[1] as number) * 256 + (encodings[2] as number);
        const body = count > 0 ? readExact(src, count * 4) : new Uint8Array(0);
        dst.write(joined(header, encodings, body));
        break;
      }

      case FRAMEBUFFER_UPDATE_REQUEST:
        dst.write(joined(header, readExact(src, UPDATE_REQUEST_BYTES)));
        // Announced only once, and only after the request is upstream: the
        // client's pixel format and encodings precede it, so a driver that
        // starts injecting requests before this would have the server answer
        // in its own format and the client render those frames with swapped
        // colours.
        if (options.onClientReady !== undefined && !clientReadyFired) {
          clientReadyFired = true;
          options.onClientReady();
        }
        break;

      case KEY_EVENT: {
        const payload = readExact(src, KEY_EVENT_BYTES);
        if (allowed(options)) {
          dst.write(joined(header, payload));
        }
        break;
      }

      case POINTER_EVENT: {
        const payload = readExact(src, POINTER_EVENT_BYTES);
        if (allowed(options)) {
          dst.write(joined(header, payload));
        }
        break;
      }

      case CLIENT_CUT_TEXT: {
        const cutHeader = readExact(src, CUT_TEXT_HEADER_BYTES);
        const declared =
          (cutHeader[3] as number) * 0x1000000 +
          (cutHeader[4] as number) * 0x10000 +
          (cutHeader[5] as number) * 0x100 +
          (cutHeader[6] as number);
        // The extended clipboard sets the top bit; reading the field whole
        // would make every extended write look like two gigabytes.
        const length = declared & EXTENDED_CLIPBOARD_MASK;

        if (length > MAX_CUT_TEXT) {
          // Drained and dropped rather than refused: raising would tear down
          // the relay and black the framebuffer for everyone watching, which
          // is a worse answer to one hostile message.
          //
          // The drain trusts the declared length, so a client that declares
          // more than it sends has its whole remaining stream consumed and
          // the session's input ends here. That is the reference's behaviour.
          let remaining = length;
          while (remaining > 0) {
            const chunk = src.read(Math.min(remaining, DRAIN_CHUNK));
            if (chunk.length === 0) {
              return;
            }
            remaining -= chunk.length;
          }
          break;
        }

        const payload = length > 0 ? readExact(src, length) : new Uint8Array(0);
        if (allowed(options)) {
          dst.write(joined(header, cutHeader, payload));
        }
        break;
      }

      default:
        throw new RfbProtocolError(`unknown RFB client message type: ${header[0]}`);
    }
  }
}
