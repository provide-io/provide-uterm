//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A transport plus terminal emulation, behind the session protocol.
 *
 * Port of the Python module `provide.uterm.transport_session`.
 *
 * The reader loop is the whole of it: pull bytes, separate any control
 * frames from the terminal output, tell everyone who is watching, and only
 * then let the emulator draw. The ordering there is deliberate and the tests
 * pin it.
 */

import { ControlFrameDecoder, type DataChunk } from "../control-channel/index.ts";
import { TerminalEmulator } from "../emulator/index.ts";
import { DEFAULT_CAPTURE_MAX_CHARS, TerminalCapture } from "./capture.ts";
import { type ExpectResult, type SendAndExpectOptions, sendAndExpect } from "./expect.ts";

/** Reinterpret latin-1 text as the byte sequence it stands for. */
function toBytes(text: string): Uint8Array {
  const bytes = new Uint8Array(text.length);
  for (let index = 0; index < text.length; index += 1) {
    bytes[index] = text.charCodeAt(index) & 0xff;
  }
  return bytes;
}

/** The transport a session reads from and writes to. */
export interface SessionTransport {
  /** Open the connection. */
  connect(): Promise<void>;
  /** Close it, unblocking any pending receive. */
  close(): Promise<void>;
  /** Write to the far end. */
  send(data: string): Promise<void>;
  /** The next chunk, or nothing when the transport has gone. */
  receive(): Promise<string | undefined>;
}

/** Options for {@link TransportSession}. */
export interface TransportSessionOptions {
  /** Where bytes come from and go to. */
  transport: SessionTransport;
  /** Whether the stream carries DLE/STX control frames inline. */
  controlChannel?: boolean;
  /** Terminal width. */
  cols?: number;
  /** Terminal height. */
  rows?: number;
}

/** A transport plus terminal emulation, behind the session protocol. */
export class TransportSession {
  readonly #transport: SessionTransport;
  readonly #emulator: TerminalEmulator;
  readonly #decoder: ControlFrameDecoder | undefined;
  readonly #watchers: Array<(snapshot: Record<string, unknown>, raw: string) => void> = [];
  readonly #controlWatchers: Array<(frame: Record<string, unknown>) => void> = [];
  #captures: TerminalCapture[] = [];
  #connected = false;
  #changeSeq = 0;
  /** Resolvers for callers waiting on the next update. */
  #waiters: Array<() => void> = [];
  #reader: Promise<void> | undefined;

  constructor(options: TransportSessionOptions) {
    this.#transport = options.transport;
    this.#emulator = new TerminalEmulator({
      ...(options.cols === undefined ? {} : { cols: options.cols }),
      ...(options.rows === undefined ? {} : { rows: options.rows }),
    });
    this.#decoder = options.controlChannel === true ? new ControlFrameDecoder() : undefined;
  }

  /** Open the transport and start reading from it. */
  async connect(): Promise<void> {
    await this.#transport.connect();
    this.#connected = true;
    this.#reader = this.#readLoop();
  }

  /** Stop reading and close the transport. Idempotent. */
  async close(): Promise<void> {
    if (!this.#connected) {
      return;
    }
    this.#connected = false;
    await this.#transport.close();
    this.#wake();
    await this.#reader;
  }

  /** Write to the far end. */
  async send(data: string): Promise<void> {
    await this.#transport.send(data);
  }

  /**
   * Send keys and wait for what they should produce.
   *
   * The reason to have this on the session rather than beside it: a caller
   * that sends first and waits afterwards can miss the answer entirely, and
   * this reads the change counter before it writes.
   */
  async sendExpect(keys: string, options: Omit<SendAndExpectOptions, "keys"> = {}): Promise<ExpectResult> {
    return sendAndExpect(this, keys, options);
  }

  /** Whether the session is still reading. */
  isConnected(): boolean {
    return this.#connected;
  }

  /** The current screen state. */
  snapshot(): Record<string, unknown> {
    return this.#emulator.getSnapshot() as unknown as Record<string, unknown>;
  }

  /** The screen with its escape sequences intact. */
  ansiScreen(): string {
    return this.#emulator.ansiScreen();
  }

  /**
   * A counter that advances on every screen update.
   *
   * Captured *before* sending, then passed to
   * {@link waitForScreenChange}, so output landing between the send and the
   * wait is not slept through.
   */
  screenChangeSeq(): number {
    return this.#changeSeq;
  }

  /** Wait for any new bytes, or time out. */
  async waitForUpdate(options: { timeoutMs: number; since?: number }): Promise<boolean> {
    return this.#awaitUpdate(options.timeoutMs);
  }

  /**
   * Wait until the screen has moved past `since`, or time out.
   *
   * With no mark, waits for the next change whenever it comes. A wait that
   * expires having still seen progress reports success rather than a stall
   * that did not happen.
   */
  async waitForScreenChange(options: { timeoutMs?: number; since?: number } = {}): Promise<boolean> {
    const timeoutMs = options.timeoutMs ?? 5000;
    const deadline = performance.now() + timeoutMs;
    const since = options.since;
    for (;;) {
      if (since !== undefined && this.#changeSeq > since) {
        return true;
      }
      const remaining = deadline - performance.now();
      if (remaining <= 0) {
        return false;
      }
      if (!(await this.#awaitUpdate(remaining))) {
        return this.#changeSeq > (since ?? 0);
      }
    }
  }

  /**
   * Watch the raw bytes as they arrive.
   *
   * Called before the emulator consumes them, so a watcher sees the wire
   * content — SGR codes and all — rather than the decoded display.
   */
  addWatch(callback: (snapshot: Record<string, unknown>, raw: string) => void): void {
    this.#watchers.push(callback);
  }

  /** Watch the control frames carried inline with the terminal output. */
  addControlFrameWatch(callback: (frame: Record<string, unknown>) => void): void {
    this.#controlWatchers.push(callback);
  }

  /**
   * Start capturing terminal text.
   *
   * The caller owns the scope and must end it. A capture left registered
   * becomes session-wide history and grows with the session rather than with
   * the operation someone asked about.
   */
  beginCapture(maxChars: number = DEFAULT_CAPTURE_MAX_CHARS): TerminalCapture {
    const capture = new TerminalCapture(maxChars);
    this.#captures.push(capture);
    return capture;
  }

  /** Stop capturing. An unknown capture is ignored. */
  endCapture(capture: TerminalCapture): void {
    this.#captures = this.#captures.filter((entry) => entry !== capture);
  }

  /** Read until the transport closes. */
  async #readLoop(): Promise<void> {
    while (this.#connected) {
      let raw: string | undefined;
      try {
        raw = await this.#transport.receive();
      } catch {
        // A dead transport ends the session rather than the process.
        this.#connected = false;
        this.#wake();
        return;
      }
      if (raw === undefined || raw === "") {
        continue;
      }
      const terminal = this.#decoder === undefined ? raw : this.#separateControlFrames(raw);
      if (terminal === undefined) {
        // The chunk was control frames only: nothing was drawn, so a caller
        // waiting for the screen to move must not be woken by it.
        continue;
      }
      for (const capture of [...this.#captures]) {
        capture.append(terminal);
      }
      for (const watcher of this.#watchers) {
        try {
          watcher({}, terminal);
        } catch {
          // A watcher is an observer; a broken one must not stop the session
          // reading its own transport.
        }
      }
      // The emulator takes bytes; the transport hands us latin-1 text, which
      // is the same byte sequence one code unit per byte.
      //
      // Drawing and bumping the counter are adjacent with no await between,
      // so their order is not observable and swapping them changes nothing.
      this.#emulator.process(toBytes(terminal));
      this.#changeSeq += 1;
      this.#wake();
    }
  }

  /**
   * Split control frames out of a chunk.
   *
   * Returns the terminal text that remains, or nothing when the chunk held
   * only control frames. Frames reaching the emulator would render raw JSON
   * onto the operator's screen.
   */
  #separateControlFrames(raw: string): string | undefined {
    const parts: string[] = [];
    for (const event of (this.#decoder as ControlFrameDecoder).feed(raw)) {
      if ("data" in event) {
        parts.push((event as DataChunk).data);
        continue;
      }
      for (const watcher of this.#controlWatchers) {
        try {
          watcher(event.control);
        } catch {
          // As above: an observer cannot break the reader.
        }
      }
    }
    return parts.length === 0 ? undefined : parts.join("");
  }

  /** Wait for the next update, reporting whether one arrived. */
  async #awaitUpdate(timeoutMs: number): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => {
        this.#waiters = this.#waiters.filter((entry) => entry !== wake);
        resolve(false);
      }, timeoutMs);
      const wake = () => {
        clearTimeout(timer);
        resolve(true);
      };
      this.#waiters.push(wake);
    });
  }

  /** Release everyone waiting on an update. */
  #wake(): void {
    const waiters = this.#waiters;
    this.#waiters = [];
    for (const resolve of waiters) {
      resolve();
    }
  }
}
