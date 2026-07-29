//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The connector that actually runs a shell.
 *
 * Port of `provide.uterm.pty.connector`. Most of the reference is operating
 * system calls; what is here is the decisions around them, which is where a
 * session goes wrong:
 *
 * * **Nothing is stored before everything is validated**, so a
 *   half-configured connector never exists.
 * * **Two pauses that must not interfere.** A hijack pause stops output and
 *   input; backpressure stops only reading, and says nothing while it does —
 *   a snapshot during congestion is traffic added to the congestion it is
 *   relieving.
 * * **The buffer is capped from the end**, so a session that has scrolled for
 *   a week costs the same and still shows the newest output.
 * * **Decoding is incremental.** A read can split a multibyte character;
 *   decoding each read alone would turn both halves into replacement
 *   characters and corrupt it permanently.
 *
 * The terminal itself is behind {@link PtyBackend}, so this can be driven
 * without one.
 */

import { createHash } from "node:crypto";
import { pyRepr } from "../pycompat/str.ts";
import { validateCommand, validateEnv, validateUsername } from "./validate.ts";

/** The input modes a session can be in. */
const VALID_MODES = new Set(["open", "hijack"]);

/** Everything a connector may be configured with. */
const VALID_CONFIG_KEYS = new Set([
  "command",
  "args",
  "username",
  "password",
  "run_as",
  "run_as_uid",
  "run_as_gid",
  "env",
  "inject",
  "cols",
  "rows",
  "input_mode",
]);

/** How much of the session is kept. */
export const BUFFER_CAP = 32768;

/** How much is asked for at a time. */
export const READ_SIZE = 4096;

/** The terminal a connector drives. */
export interface PtyBackend {
  /**
   * Bytes that have arrived and not yet been taken.
   *
   * Three answers, matching the three a non-blocking descriptor gives:
   * bytes, `undefined` for nothing yet (the reference's `EAGAIN`), and an
   * empty array for the end of the child. The distinction matters: a terminal
   * that reported "nothing yet" as an ending would end every session in the
   * moment before its first output.
   */
  read(): Uint8Array | undefined;
  /** Send bytes to the child. */
  write(data: Uint8Array): void;
  /** Whether the child is still there. */
  isAlive(): boolean;
  /** End the child and release the terminal. */
  close(): Promise<void>;
}

/** What a connector is asked to run. */
export type PtyConfig = Record<string, unknown>;

/** A frame a connector produces. */
export interface ConnectorMessage {
  [key: string]: unknown;
  type: string;
}

/**
 * A shell behind a pseudo-terminal.
 *
 * @throws {Error} From the constructor, when the configuration is not one this
 *   can run — before anything is stored.
 */
export class PtyConnector {
  readonly sessionId: string;
  readonly displayName: string;
  readonly command: string;
  readonly args: string[];
  readonly cols: number;
  readonly rows: number;
  readonly inject: boolean;

  private mode: string;
  private backend: PtyBackend | undefined;
  private connected = false;
  private paused = false;
  /**
   * Backpressure, deliberately separate from {@link paused}.
   *
   * The hub signalling congestion must not clear a hijack, and a hijack must
   * not clear congestion; one flag for both would let either do it.
   */
  private flowPaused = false;
  private buffer = "";
  /**
   * Held across reads, so a character split by a read boundary completes
   * rather than becoming two replacement characters.
   */
  private readonly decoder = new TextDecoder("utf-8", { fatal: false });

  constructor(sessionId: string, displayName: string, config: PtyConfig) {
    const unknown = Object.keys(config)
      .filter((key) => !VALID_CONFIG_KEYS.has(key))
      .sort();
    if (unknown.length > 0) {
      throw new Error(`unknown config keys for PTYConnector: ${JSON.stringify(unknown)}`);
    }
    if (!Object.hasOwn(config, "command")) {
      throw new Error("PTYConnector requires 'command' in connector_config");
    }

    // Everything is checked before anything is kept.
    validateCommand(String(config.command));
    if (config.username !== undefined && config.username !== "") {
      validateUsername(String(config.username));
    }
    if (config.env !== undefined && config.env !== null && Object.keys(config.env).length > 0) {
      validateEnv(config.env as Record<string, string>);
    }
    if (config.input_mode !== undefined && config.input_mode !== null && !VALID_MODES.has(String(config.input_mode))) {
      throw new Error(
        `invalid input_mode ${JSON.stringify(config.input_mode)}: must be one of ${JSON.stringify([...VALID_MODES].sort())}`,
      );
    }

    this.sessionId = sessionId;
    this.displayName = displayName;
    this.command = String(config.command);
    this.args = ((config.args as string[] | undefined) ?? []).map(String);
    this.cols = Number(config.cols ?? 80);
    this.rows = Number(config.rows ?? 24);
    // By truth, not identity: the reference's `bool(...)`, so a config
    // written by hand or read from TOML saying 1 or "yes" turns it on.
    this.inject = Boolean(config.inject);
    this.mode = String(config.input_mode ?? "open");
  }

  /** Attach a terminal that has been started. */
  attach(backend: PtyBackend): void {
    this.backend = backend;
    this.connected = true;
  }

  /** Whether there is a live terminal behind this. */
  isConnected(): boolean {
    return this.connected && this.backend !== undefined;
  }

  /** The current input mode. */
  inputMode(): string {
    return this.mode;
  }

  /** Take whatever the child has produced. */
  async pollMessages(): Promise<ConnectorMessage[]> {
    if (!this.isConnected() || this.paused || this.flowPaused) {
      return [];
    }
    const data = this.readFromBackend();
    if (data.length === 0) {
      return [];
    }
    this.buffer += this.decodeIncrementally(data);
    // `>` rather than `>=`, which cannot differ: slicing the last BUFFER_CAP
    // characters off a string of exactly that length returns it unchanged.
    if (this.buffer.length > BUFFER_CAP) {
      // From the end: the newest output is the output somebody is looking at.
      this.buffer = this.buffer.slice(-BUFFER_CAP);
    }
    return [this.snapshot()];
  }

  /** Send what somebody typed to the child. */
  async handleInput(data: string): Promise<ConnectorMessage[]> {
    if (this.isConnected() && !this.paused) {
      try {
        (this.backend as PtyBackend).write(new TextEncoder().encode(data));
      } catch {
        // The terminal fails the instant the child exits. Treated as the child
        // going away rather than raised into the run loop as an unclean error.
        this.connected = false;
      }
    }
    return [this.snapshot()];
  }

  /** Pause, resume, or apply backpressure. */
  async handleControl(action: string): Promise<ConnectorMessage[]> {
    if (action === "pause") {
      this.paused = true;
    } else if (action === "resume" || action === "step") {
      this.paused = false;
    } else if (action === "flow_pause") {
      this.flowPaused = true;
      // Nothing said: a snapshot here is traffic added to the congestion this
      // is relieving.
      return [];
    } else if (action === "flow_resume") {
      this.flowPaused = false;
      return [];
    }
    return [this.snapshot()];
  }

  /** What the session looks like now. */
  async getSnapshot(): Promise<ConnectorMessage> {
    return this.snapshot();
  }

  /** A line describing the connector, for an operator. */
  async getAnalysis(): Promise<string> {
    return (
      `PTYConnector command=${pyRepr(this.command)} ` +
      `connected=${this.connected ? "True" : "False"} paused=${this.paused ? "True" : "False"} ` +
      `inject=${this.inject ? "True" : "False"} cols=${this.cols} rows=${this.rows} ` +
      `buffer_len=${this.buffer.length}`
    );
  }

  /**
   * Change the input mode.
   *
   * @throws {Error} For a mode nobody defined.
   */
  async setMode(mode: string): Promise<ConnectorMessage[]> {
    if (!VALID_MODES.has(mode)) {
      throw new Error(`invalid mode ${pyRepr(mode)}: must be one of ${JSON.stringify([...VALID_MODES].sort())}`);
    }
    this.mode = mode;
    return [this.hello(), this.snapshot()];
  }

  /**
   * Forget what has been shown.
   *
   * The decoder is deliberately left alone: a character straddling this should
   * still complete on the next read rather than lose the bytes already taken.
   */
  async clear(): Promise<ConnectorMessage[]> {
    this.buffer = "";
    return [this.snapshot()];
  }

  /**
   * End the child and release the terminal.
   *
   * Both the flag and the reference are dropped. Either alone would be
   * unobservable — {@link isConnected} wants both — but keeping the terminal
   * leaks a descriptor, and keeping the flag would make a connector claim a
   * session it no longer has.
   */
  async stop(): Promise<void> {
    if (this.backend !== undefined) {
      await this.backend.close();
      this.backend = undefined;
    }
    this.connected = false;
  }

  /** Read, noticing the child going away however that arrives. */
  private readFromBackend(): Uint8Array {
    const data = (this.backend as PtyBackend).read();
    if (data === undefined) {
      // Nothing yet, which is not an ending.
      return new Uint8Array();
    }
    if (data.length === 0) {
      // The end of the child: on a non-blocking descriptor this is the only
      // thing an empty read can mean.
      this.connected = false;
    }
    return data;
  }

  /**
   * Decode, holding back any trailing bytes of an unfinished character.
   *
   * `stream: true` keeps a trailing partial sequence inside the decoder, so
   * the next call completes the character rather than replacing both halves
   * with U+FFFD. The state lives in the decoder, which is why `clear` can
   * forget the screen without disturbing a character mid-flight.
   */
  private decodeIncrementally(data: Uint8Array): string {
    return this.decoder.decode(data, { stream: true });
  }

  /** The frame describing the session. */
  private snapshot(): ConnectorMessage {
    const screen = this.buffer;
    return {
      type: "snapshot",
      screen,
      cursor: { row: 0, col: 0 },
      cols: this.cols,
      rows: this.rows,
      // Change detection, not a security property.
      screen_hash: createHash("md5").update(screen, "utf8").digest("hex"),
      cursor_at_end: true,
      has_trailing_space: false,
      prompt_detected: false,
    };
  }

  /** The frame announcing the input mode. */
  private hello(): ConnectorMessage {
    return { type: "worker_hello", input_mode: this.mode };
  }
}
