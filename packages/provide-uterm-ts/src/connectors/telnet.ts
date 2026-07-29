//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A hosted session backed by a telnet endpoint.
 *
 * Port of `provide.uterm.server.connectors.telnet`.
 *
 * Two things here are load-bearing beyond moving bytes. The settings are a
 * closed set, so a mistyped key is refused by name rather than silently doing
 * nothing — which matters because the `[[sessions]]` entry that carries them
 * *folds* unrecognised keys in rather than refusing them, and this is where
 * that fold is caught. And the address actually reached is checked again once
 * the connection is up: the guard at create time saw a name, and a name can
 * resolve to one host and connect to another.
 */

import { MAX_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION, PREFERRED_PROTOCOL_VERSION } from "../bridge/index.ts";
import { TELNET_HOST, TELNET_REMOTE_PORT } from "../defaults/index.ts";
import { assertIpAllowed } from "../egress/index.ts";
import { pyInt } from "../pycompat/index.ts";
import { decodeCp437, encodeCp437 } from "../screen/index.ts";
import type { SessionConnector, WorkerMessage } from "./base.ts";
import {
  boundedBuffer,
  controlBanner,
  modeBanner,
  OVERLAY_COLS,
  OVERLAY_ROWS,
  overlayScreen,
  overlaySnapshot,
  pyBool,
  pyTruthy,
  rejectUnknownConnectorKeys,
} from "./overlay.ts";

/** The screen the overlay is drawn for. */
export const TELNET_COLS = OVERLAY_COLS;
export const TELNET_ROWS = OVERLAY_ROWS;

/** How much is read at a time, and how long a read waits. */
const READ_SIZE = 4096;
const READ_TIMEOUT_MS = 100;

/** The settings this connector will take. */
export const TELNET_CONNECTOR_KEYS: ReadonlySet<string> = new Set([
  "host",
  "port",
  "input_mode",
  "hub_overlay",
  "block_private_connector_targets",
]);

/** What the connector needs of a transport, which is all it uses. */
export interface TelnetConnectorTransport {
  connect(host: string, port: number): Promise<void>;
  disconnect(): Promise<void>;
  isConnected(): boolean;
  /** The address actually reached, or nothing when it could not be determined. */
  peerIp(): string | undefined;
  receive(size: number, timeoutMs: number): Promise<Uint8Array>;
  send(data: Uint8Array): Promise<void>;
}

/** What a caller may supply in place of the real world. */
export interface TelnetConnectorOptions {
  transport: TelnetConnectorTransport;
  /** Seconds, as the reference's `time.time()` gives them. */
  now?: () => number;
}

/** A connection failure the transport reports. */
export class TelnetConnectorReceiveError extends Error {}

/**
 * Connect a hosted session to a remote telnet endpoint.
 *
 * @throws {Error} On construction, when the settings carry a name this
 *   connector does not have.
 */
export class TelnetSessionConnector implements SessionConnector {
  readonly #sessionId: string;
  readonly #displayName: string;
  readonly #host: string;
  readonly #port: number;
  readonly #transport: TelnetConnectorTransport;
  readonly #now: () => number;
  readonly #hubOverlay: boolean;
  readonly #blockPrivate: boolean;
  #connected = false;
  #inputMode: string;
  #paused = false;
  #receivedBytes = 0;
  #screenBuffer = "";
  #banner: string;

  constructor(
    sessionId: string,
    displayName: string,
    config: Readonly<Record<string, unknown>>,
    options: TelnetConnectorOptions,
  ) {
    rejectUnknownConnectorKeys("telnet", config, TELNET_CONNECTOR_KEYS);
    this.#sessionId = sessionId;
    this.#displayName = displayName;
    this.#host = String(config.host ?? TELNET_HOST);
    // The reference's `int()`, which refuses `"23.5"` rather than truncating
    // it: a port written wrong is a config a server should not start on, not
    // one that quietly listens somewhere else.
    const port = pyInt(config.port ?? TELNET_REMOTE_PORT);
    if (port === undefined) {
      throw new Error(`invalid literal for int() with base 10: '${String(config.port)}'`);
    }
    this.#port = port;
    this.#transport = options.transport;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#inputMode = String(config.input_mode ?? "open");
    // Absent means on: a session with no overlay setting still says who it is
    // and what it is connected to.
    this.#hubOverlay = config.hub_overlay === undefined ? true : pyTruthy(config.hub_overlay);
    this.#blockPrivate = pyTruthy(config.block_private_connector_targets);
    this.#banner = `Connected to telnet://${this.#host}:${this.#port}`;
  }

  #snapshot(): WorkerMessage {
    const screen = overlayScreen({
      sessionId: this.#sessionId,
      displayName: this.#displayName,
      upstream: `telnet://${this.#host}:${this.#port}`,
      inputMode: this.#inputMode,
      paused: this.#paused,
      banner: this.#banner,
      buffer: this.#screenBuffer,
      hubOverlay: this.#hubOverlay,
    });
    return overlaySnapshot(screen, "telnet_stream", this.#now());
  }

  /**
   * Connect, then check the address actually reached.
   *
   * The guard at create time saw a name. This sees the host, which is the only
   * thing that stops a name resolving to one address and connecting to
   * another. An address nobody could determine is not itself a refusal — this
   * only ever aborts on one positively identified as blocked.
   *
   * @throws {EgressBlockedError} On an address the policy refuses, after
   *   hanging up.
   */
  async start(): Promise<void> {
    await this.#transport.connect(this.#host, this.#port);
    const peerIp = this.#transport.peerIp();
    if (peerIp !== undefined && peerIp !== "") {
      try {
        assertIpAllowed(peerIp, { blockPrivate: this.#blockPrivate });
      } catch (error) {
        await this.#transport.disconnect().catch(() => undefined);
        throw error;
      }
    }
    this.#connected = true;
  }

  async stop(): Promise<void> {
    await this.#transport.disconnect();
    this.#connected = false;
  }

  isConnected(): boolean {
    return this.#connected && this.#transport.isConnected();
  }

  /**
   * Whatever the endpoint has sent, as terminal output and a new snapshot.
   *
   * A read that fails ends the session quietly rather than raising: the poll
   * loop is what keeps every viewer's screen current, and a raise there would
   * take the loop down with it.
   */
  async pollMessages(): Promise<WorkerMessage[]> {
    if (!this.isConnected()) {
      return [];
    }
    let data: Uint8Array;
    try {
      data = await this.#transport.receive(READ_SIZE, READ_TIMEOUT_MS);
    } catch {
      await this.#transport.disconnect().catch(() => undefined);
      this.#connected = false;
      return [];
    }
    if (data.length === 0) {
      return [];
    }
    this.#receivedBytes += data.length;
    // Read as CP437: a telnet endpoint old enough to need this connector draws
    // its boxes with high bytes, and reading them as UTF-8 would replace every
    // one of them.
    const text = decodeCp437(data);
    this.#screenBuffer = boundedBuffer(this.#screenBuffer, text);
    this.#banner = `Received ${this.#receivedBytes} bytes from telnet upstream.`;
    return [{ type: "term", data: text, ts: this.#now() }, this.#snapshot()];
  }

  async handleInput(data: string): Promise<WorkerMessage[]> {
    if (this.isConnected()) {
      await this.#transport.send(encodeCp437(data));
      this.#banner = `Sent ${data.length} characters upstream.`;
    }
    return [this.#snapshot()];
  }

  async handleControl(action: string): Promise<WorkerMessage[]> {
    if (action === "pause") {
      this.#paused = true;
    } else if (action === "resume") {
      this.#paused = false;
    }
    this.#banner = controlBanner(action);
    return [this.#snapshot()];
  }

  /** The hello frame that tells the far end the mode and the protocol range. */
  #hello(): WorkerMessage {
    return {
      type: "worker_hello",
      input_mode: this.#inputMode,
      ts: this.#now(),
      protocol: { min: MIN_PROTOCOL_VERSION, max: MAX_PROTOCOL_VERSION, preferred: PREFERRED_PROTOCOL_VERSION },
    };
  }

  async getSnapshot(): Promise<WorkerMessage> {
    return this.#snapshot();
  }

  async getAnalysis(): Promise<string> {
    return [
      `[telnet session analysis — worker: ${this.#sessionId}]`,
      `host: ${this.#host}`,
      `port: ${this.#port}`,
      `input_mode: ${this.#inputMode}`,
      `paused: ${pyBool(this.#paused)}`,
      `bytes_received: ${this.#receivedBytes}`,
      `connected: ${pyBool(this.isConnected())}`,
    ].join("\n");
  }

  async clear(): Promise<WorkerMessage[]> {
    this.#screenBuffer = "";
    this.#banner = "Screen buffer cleared.";
    return [this.#snapshot()];
  }

  /**
   * Change who may type.
   *
   * Returning to shared input releases the hold as well, so a session cannot
   * be left shared but paused with nobody holding it.
   *
   * @throws {Error} On a mode the session does not have.
   */
  async setMode(mode: string): Promise<WorkerMessage[]> {
    if (mode !== "open" && mode !== "hijack") {
      throw new Error(`invalid mode: ${mode}`);
    }
    this.#inputMode = mode;
    if (mode === "open") {
      this.#paused = false;
    }
    this.#banner = modeBanner(mode);
    return [this.#hello(), this.#snapshot()];
  }
}
