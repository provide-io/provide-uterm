//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Stream adapters for an SSH session.
 *
 * Port of the adapter half of the Python module
 * `provide.uterm.transports.ssh`.
 *
 * An SSH server hands a session two halves of a process; the rest of the
 * platform speaks bytes to a reader and a writer. Every decision here is
 * about what to do when the connection underneath has already gone — and the
 * answer is always the same: report the end of the stream, never raise into a
 * session that has nobody left to catch it.
 */

import { pyEncodeReplace } from "../pycompat/index.ts";

/** No bytes, shared rather than allocated per end-of-stream. */
const EMPTY = new Uint8Array(0);

/** The process an SSH session is given. */
export interface SshProcess {
  stdin: { read(size?: number): Promise<unknown> };
  stdout: { write(data: Uint8Array): void; drain(): Promise<void> };
  exit(code: number): void;
  close(): void;
  getExtraInfo(name: string): unknown;
}

/**
 * Adapts a session's input to a byte reader.
 *
 * Nothing it can be handed is an error. A dropped connection, a cancelled
 * read and a clean end-of-file all reach the caller as "no more bytes",
 * because the caller's next move is the same for all three.
 */
export class SshStreamReader {
  readonly #process: SshProcess;

  constructor(process: SshProcess) {
    this.#process = process;
  }

  /** Read what is available, or nothing once the session has ended. */
  async read(size = -1): Promise<Uint8Array> {
    let data: unknown;
    try {
      data = await this.#process.stdin.read(size);
    } catch {
      return EMPTY;
    }
    if (typeof data === "string") {
      // A channel negotiated with an encoding hands back text. Encoded rather
      // than refused — the platform below deals in bytes, and refusing would
      // drop a live session over a negotiation detail. A character that
      // cannot be encoded becomes a question mark, which is what the
      // reference's `errors="replace"` writes on the way out.
      return Uint8Array.from(pyEncodeReplace(data));
    }
    if (data instanceof Uint8Array) {
      return data;
    }
    // Anything else is not bytes and not text, so there is nothing to hand
    // on; treated as the end of the stream rather than guessed at.
    return EMPTY;
  }
}

/**
 * Adapts a session's output to a byte writer.
 *
 * Once closed it stays closed and every call is a silent no-op: the session
 * that closed it has already moved on, and an error raised there has nobody
 * to catch it.
 */
export class SshStreamWriter {
  readonly #process: SshProcess;
  #closed = false;

  constructor(process: SshProcess) {
    this.#process = process;
  }

  /** Send bytes, closing the writer if the channel has gone. */
  write(data: Uint8Array): void {
    if (this.#closed) {
      return;
    }
    try {
      this.#process.stdout.write(data);
    } catch {
      // There is nowhere left to send anything, and continuing to try would
      // raise once per frame for the life of the session.
      this.close();
    }
  }

  /** Flush what has been written, closing the writer if the channel has gone. */
  async drain(): Promise<void> {
    if (this.#closed) {
      return;
    }
    try {
      await this.#process.stdout.drain();
    } catch {
      this.close();
    }
  }

  /**
   * End the session.
   *
   * Best-effort on both halves: the connection may already be gone, and the
   * writer has to end up closed either way. The exit status goes first
   * because a session that ends without one leaves the client waiting.
   */
  close(): void {
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    try {
      this.#process.exit(0);
    } catch {
      // Already gone; there is nothing to report an exit status to.
    }
    try {
      this.#process.close();
    } catch {
      // Likewise.
    }
  }

  /** Nothing to wait for: the runtime manages its own lifecycle. */
  async waitClosed(): Promise<void> {
    return undefined;
  }

  /**
   * What is known about the other end.
   *
   * Only the peer address, and only when there is one — an empty address is
   * no address, and reporting it would put a blank where an audit line
   * expects a host.
   */
  getExtraInfo(name: string, fallback?: unknown): unknown {
    if (name === "peername") {
      const peer = this.#process.getExtraInfo("peername");
      if (peer !== null && peer !== undefined && (!Array.isArray(peer) || peer.length > 0)) {
        return peer;
      }
    }
    return fallback;
  }
}
