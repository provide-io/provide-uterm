//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Captured terminal traffic, as it arrives from the capture library.
 *
 * Port of `provide.uterm.pty.capture`. Frames come off a Unix socket as
 * `[1B channel][4B length, big-endian][payload]`, and two rules around that
 * framing are why this is worth porting carefully rather than re-deriving.
 *
 * **The length cap is checked before the body is read.** A producer claiming
 * four gigabytes is refused *without* the read being attempted; checking
 * afterwards would mean allocating what was claimed, which is the whole
 * attack. The connection is dropped rather than the frame skipped, because a
 * length that large means the stream is no longer trustworthy — and once one
 * frame's length is wrong there is no way to find where the next one starts.
 *
 * **The queue drops its oldest frame, not its newest.** A producer faster than
 * its reader cannot grow memory without bound, and what a viewer wants when
 * something has to go is the most recent screen rather than the stalest.
 */

/**
 * Which stream a frame came from.
 *
 * A reader passes on a channel it does not know rather than refusing it, so a
 * producer may send a newer one to an older reader.
 */
export const CHANNEL_STDOUT = 0x01;
export const CHANNEL_STDIN = 0x02;
export const CHANNEL_CONNECT = 0x03;
/** The capture shim's own writer counters, as an ASCII `key=value` line. */
export const CHANNEL_STATS = 0x04;
/**
 * The terminal's size, from whoever owns the pty (a supervisor): cols then
 * rows, each a big-endian `uint16`. The LD_PRELOAD shim sends it too — from
 * TIOCGWINSZ, on connect and at most once per 250 ms on the write path, when
 * the size changed.
 */
export const CHANNEL_GEOMETRY = 0x05;
/**
 * The producer's keystroke-socket path, as a UTF-8 absolute path. An empty
 * payload withdraws the path. Sent only by a producer that can deliver
 * keystrokes (a supervisor) — never the LD_PRELOAD shim, which has no
 * keystroke socket to name. A reader that predates it ignores it, as it does
 * any channel it does not know, so a producer may send it unconditionally.
 */
export const CHANNEL_INPUT = 0x06;

/** A geometry payload is exactly this long. */
export const GEOMETRY_PAYLOAD_SIZE = 4;
/** The smallest and largest cols or rows a geometry frame may name, inclusive. */
export const GEOMETRY_MIN = 1;
export const GEOMETRY_MAX = 1000;

/** The largest keystroke-socket path an input frame may carry, in bytes. */
export const INPUT_MAX_LEN = 4096;

/** A terminal size, as a geometry frame names it. */
export interface CaptureGeometry {
  cols: number;
  rows: number;
}

/**
 * The size a geometry payload names, or `null` for anything malformed.
 *
 * Malformed is ignored rather than fatal: the frame's own length kept the
 * stream in step, so only this one frame is untrustworthy.
 */
export function parseGeometry(payload: Uint8Array): CaptureGeometry | null {
  if (payload.length !== GEOMETRY_PAYLOAD_SIZE) {
    return null;
  }
  const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const cols = view.getUint16(0, false);
  const rows = view.getUint16(2, false);
  if (cols < GEOMETRY_MIN || cols > GEOMETRY_MAX || rows < GEOMETRY_MIN || rows > GEOMETRY_MAX) {
    return null;
  }
  return { cols, rows };
}

/**
 * The keystroke-socket path an input payload names, or `null` for anything
 * malformed.
 *
 * An empty payload withdraws the path (returns `""`); a non-empty payload is
 * a UTF-8 absolute path. Non-UTF-8, a path over `INPUT_MAX_LEN`, or a path that
 * is not absolute returns `null` — the caller ignores the frame, since the
 * frame's own length kept the stream in step and only this one is untrustworthy.
 *
 * The returned path is **not** validated against the capture-socket convention
 * (`.sock` → `.stdin.sock`); that is the connector's job, since it depends on
 * the capture socket the session was created with. This port carries framing
 * only, as for geometry.
 */
export function parseInputPath(payload: Uint8Array): string | null {
  if (payload.length > INPUT_MAX_LEN) {
    return null;
  }
  if (payload.length === 0) {
    return "";
  }
  // Decode as UTF-8. TextDecoder with fatal: true throws on invalid UTF-8.
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let path: string;
  try {
    path = decoder.decode(payload);
  } catch {
    return null;
  }
  if (!path.startsWith("/")) {
    return null;
  }
  return path;
}

/** Channel byte plus a four-byte length. */
export const CAPTURE_HEADER_SIZE = 5;

/**
 * The largest frame that will be read.
 *
 * Inclusive: a frame of exactly this size is read, and one byte more is
 * refused.
 */
export const MAX_CAPTURE_FRAME_BYTES = 16 * 1024 * 1024;

/** How many frames are held for a reader that has fallen behind. */
export const CAPTURE_QUEUE_MAXSIZE = 4096;

/** The mode the capture socket is created with by default, and nothing wider. */
export const CAPTURE_SOCKET_MODE = 0o600;

/**
 * Every mode a capture socket may be created with: owner-only, or owner and
 * group, for a server that shares its capture sockets with producers running
 * as another user through a setgid directory's group. A world bit would hand
 * the terminal to every local user, so no wider mode is ever accepted.
 */
export const CAPTURE_SOCKET_MODES: readonly number[] = [CAPTURE_SOCKET_MODE, 0o660];

/**
 * The umask the socket is bound under.
 *
 * Bound under it rather than chmod'd after: a post-bind `chmod` leaves a
 * window in which the socket exists at the default mode and any local user can
 * connect to it — and this socket carries everything typed and shown.
 */
export const CAPTURE_BIND_UMASK = 0o177;

/** One frame off the capture socket. */
export interface CaptureFrame {
  channel: number;
  data: Uint8Array;
}

/** The stream ended before the bytes that were asked for. */
export class CaptureShortRead extends Error {}

/** Where the frames come from. */
export interface CaptureReader {
  /**
   * Exactly `size` bytes.
   *
   * @throws {CaptureShortRead} When the stream ends first.
   */
  readExactly(size: number): Promise<Uint8Array>;
}

/**
 * Read frames until the stream ends or says something impossible.
 *
 * Ends quietly on a short read — that is how a capture session ends — and on
 * an over-large claim, which is how one is abandoned.
 *
 * @param onFrame Called for each frame, in order.
 * @returns Why the reading stopped.
 */
export async function readCaptureFrames(
  reader: CaptureReader,
  onFrame: (frame: CaptureFrame) => void,
): Promise<"ended" | "frame-too-large"> {
  for (;;) {
    let header: Uint8Array;
    try {
      header = await reader.readExactly(CAPTURE_HEADER_SIZE);
    } catch (error) {
      if (error instanceof CaptureShortRead) {
        return "ended";
      }
      throw error;
    }
    const view = new DataView(header.buffer, header.byteOffset, header.byteLength);
    const channel = view.getUint8(0);
    const length = view.getUint32(1, false);

    if (length > MAX_CAPTURE_FRAME_BYTES) {
      // Refused here, before the read: asking for the body first is what
      // would let a claimed four gigabytes actually be allocated.
      return "frame-too-large";
    }

    let data: Uint8Array;
    try {
      data = await reader.readExactly(length);
    } catch (error) {
      if (error instanceof CaptureShortRead) {
        return "ended";
      }
      throw error;
    }
    onFrame({ channel, data });
  }
}

/**
 * A bounded queue of captured frames.
 *
 * Full means the oldest goes. A reader that has fallen behind gets the most
 * recent screen rather than the stalest, and a producer cannot grow this
 * without bound whatever it does.
 */
export class CaptureQueue {
  readonly #frames: CaptureFrame[] = [];
  readonly #maxsize: number;
  #dropped = 0;

  constructor(maxsize: number = CAPTURE_QUEUE_MAXSIZE) {
    this.#maxsize = maxsize;
  }

  /** How many frames are waiting. */
  get size(): number {
    return this.#frames.length;
  }

  /** How many were dropped to make room, since this queue was made. */
  get dropped(): number {
    return this.#dropped;
  }

  /** Add a frame, dropping the oldest if there is no room. */
  push(frame: CaptureFrame): void {
    if (this.#frames.length >= this.#maxsize) {
      this.#frames.shift();
      this.#dropped += 1;
    }
    this.#frames.push(frame);
  }

  /** The next frame, or nothing when there is none waiting. */
  pop(): CaptureFrame | undefined {
    return this.#frames.shift();
  }
}
