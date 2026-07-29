//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A hosted session backed by a WebSocket endpoint.
 *
 * Port of `provide.uterm.server.connectors.websocket`, the sibling of the
 * telnet connector.
 *
 * The endpoint is required and its scheme is checked at construction, so a
 * config mistake is a server that will not start rather than a session that
 * exists and never works. And, as with telnet, the address actually reached is
 * checked once the connection is up: TLS completed against the *name*, and a
 * name with a zero TTL can point somewhere else by the time the socket opens.
 */

import { MAX_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION, PREFERRED_PROTOCOL_VERSION } from "../bridge/index.ts";
import { assertIpAllowed } from "../egress/index.ts";
import { decodeCp437 } from "../screen/index.ts";
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
  rejectUnknownConnectorKeys,
} from "./overlay.ts";

/** The screen the overlay is drawn for. */
export const WS_COLS = OVERLAY_COLS;
export const WS_ROWS = OVERLAY_ROWS;

/** The settings this connector will take. */
export const WS_CONNECTOR_KEYS: ReadonlySet<string> = new Set([
  "url",
  "input_mode",
  "hub_overlay",
  "block_private_connector_targets",
]);

/** The schemes a WebSocket endpoint may use. */
const WS_SCHEMES: ReadonlySet<string> = new Set(["ws", "wss"]);

/** One frame off the wire: text as it was sent, or bytes to be decoded. */
export type WsFrame = { binary: true; data: Uint8Array } | { binary: false; data: string };

/** Nothing arrived before the read gave up, which is not a disconnection. */
export class WsReadTimeout extends Error {}

/** What the connector needs of a socket, which is all it uses. */
export interface WsConnectorSocket {
  /**
   * The next frame.
   *
   * @throws {WsReadTimeout} When nothing arrived in time.
   * @throws {Error} When the socket has gone.
   */
  receive(): Promise<WsFrame>;
  send(data: string): Promise<void>;
  close(): Promise<void>;
  /** The address actually reached, or nothing when it could not be determined. */
  peerIp(): string | undefined;
}

/** What a caller may supply in place of the real world. */
export interface WsConnectorOptions {
  /** Opens a socket to the endpoint. */
  connect: (url: string) => Promise<WsConnectorSocket>;
  /** Seconds, as the reference's `time.time()` gives them. */
  now?: () => number;
}

/**
 * Connect a hosted session to a remote WebSocket endpoint.
 *
 * @throws {Error} On construction, when the settings carry a name this
 *   connector does not have, or no usable endpoint.
 */
export class WebSocketSessionConnector implements SessionConnector {
  readonly #sessionId: string;
  readonly #displayName: string;
  readonly #url: string;
  readonly #connect: (url: string) => Promise<WsConnectorSocket>;
  readonly #now: () => number;
  readonly #hubOverlay: boolean;
  readonly #blockPrivate: boolean;
  #socket: WsConnectorSocket | undefined;
  #inputMode: string;
  #paused = false;
  #receivedBytes = 0;
  #screenBuffer = "";
  #banner: string;

  constructor(
    sessionId: string,
    displayName: string,
    config: Readonly<Record<string, unknown>>,
    options: WsConnectorOptions,
  ) {
    rejectUnknownConnectorKeys("websocket", config, WS_CONNECTOR_KEYS);
    if (config.url === undefined || config.url === null) {
      throw new Error("websocket connector requires connector_config.url");
    }
    const url = String(config.url);
    const parsed = parseWsUrl(url);
    if (!WS_SCHEMES.has(parsed.scheme)) {
      // Refused here rather than on connect: a session that exists and never
      // works is harder to notice than a server that will not start.
      throw new Error("websocket connector_config.url scheme must be ws or wss");
    }
    if (parsed.host === "") {
      throw new Error("websocket connector_config.url must include a host");
    }
    this.#sessionId = sessionId;
    this.#displayName = displayName;
    this.#url = url;
    this.#connect = options.connect;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#inputMode = String(config.input_mode ?? "open");
    // Absent means on: a session with no overlay setting still says who it is
    // and what it is connected to.
    this.#hubOverlay = config.hub_overlay === undefined ? true : Boolean(config.hub_overlay);
    this.#blockPrivate = Boolean(config.block_private_connector_targets);
    this.#banner = `Connecting to ${this.#url}`;
  }

  #snapshot(): WorkerMessage {
    const screen = overlayScreen({
      sessionId: this.#sessionId,
      displayName: this.#displayName,
      upstream: this.#url,
      inputMode: this.#inputMode,
      paused: this.#paused,
      banner: this.#banner,
      buffer: this.#screenBuffer,
      hubOverlay: this.#hubOverlay,
    });
    return overlaySnapshot(screen, "ws_stream", this.#now());
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

  /**
   * Open the socket, then check the address actually reached.
   *
   * The handshake completed against the endpoint's *name*, so certificate
   * validation is untouched by this — but a name with a zero TTL can point
   * somewhere else by the time the socket opens, and this is what sees where
   * it actually went. An address nobody could determine is not itself a
   * refusal.
   *
   * @throws {EgressBlockedError} On an address the policy refuses, after
   *   closing the socket.
   */
  async start(): Promise<void> {
    const socket = await this.#connect(this.#url);
    const peerIp = socket.peerIp();
    if (peerIp !== undefined && peerIp !== "") {
      try {
        assertIpAllowed(peerIp, { blockPrivate: this.#blockPrivate });
      } catch (error) {
        await socket.close().catch(() => undefined);
        throw error;
      }
    }
    this.#socket = socket;
    this.#banner = `Connected to ${this.#url}`;
  }

  async stop(): Promise<void> {
    if (this.#socket !== undefined) {
      await this.#socket.close().catch(() => undefined);
      this.#socket = undefined;
    }
  }

  /**
   * Whether this session is live.
   *
   * Holding the socket is the whole of it. The reference keeps a separate flag
   * alongside, but the two can never disagree — every path that clears one
   * clears the other — so there is nothing here for a second one to say.
   */
  isConnected(): boolean {
    return this.#socket !== undefined;
  }

  /**
   * Whatever the endpoint has sent, as terminal output and a new snapshot.
   *
   * Nothing to read is not a disconnection — it is what a quiet session looks
   * like — so a read that times out says nothing at all. Anything else means
   * the socket has gone, which is said once and leaves the session down.
   */
  async pollMessages(): Promise<WorkerMessage[]> {
    const socket = this.#socket;
    if (socket === undefined) {
      return [];
    }
    let frame: WsFrame;
    try {
      frame = await socket.receive();
    } catch (error) {
      if (error instanceof WsReadTimeout) {
        return [];
      }
      this.#banner = "WebSocket connection closed.";
      this.#socket = undefined;
      await socket.close().catch(() => undefined);
      return [this.#snapshot()];
    }

    let text: string;
    if (frame.binary) {
      // Bytes are read as CP437 for the same reason telnet's are, and counted
      // as they arrived.
      text = decodeCp437(frame.data);
      this.#receivedBytes += frame.data.length;
    } else {
      text = frame.data;
      // Counted in UTF-8 bytes rather than characters, so a viewer's byte
      // count means the same thing for either kind of frame.
      this.#receivedBytes += new TextEncoder().encode(frame.data).length;
    }

    this.#screenBuffer = boundedBuffer(this.#screenBuffer, text);
    this.#banner = `Received ${this.#receivedBytes} bytes from WebSocket upstream.`;
    return [{ type: "term", data: text, ts: this.#now() }, this.#snapshot()];
  }

  async handleInput(data: string): Promise<WorkerMessage[]> {
    if (this.#socket !== undefined) {
      // Sent as a text frame: a WebSocket endpoint has its own framing, so
      // there is nothing to encode it into.
      await this.#socket.send(data);
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

  async getSnapshot(): Promise<WorkerMessage> {
    return this.#snapshot();
  }

  async getAnalysis(): Promise<string> {
    return [
      `[websocket session analysis — worker: ${this.#sessionId}]`,
      `url: ${this.#url}`,
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

/**
 * The scheme and host of a URL, as the reference's `urlparse` reads them.
 *
 * `URL` is not used: it refuses a string with no scheme outright, where the
 * reference reads that as an empty scheme and then refuses it by name — and
 * the two produce different messages for the same bad config.
 *
 * Both parts come back lowercased and the host without its brackets, which is
 * what `urlparse().hostname` gives.
 */
export function parseWsUrl(url: string): { scheme: string; host: string } {
  const match = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/?#]*)/.exec(url);
  if (match === null) {
    return { scheme: "", host: "" };
  }
  const authority = match[2] as string;
  // Strip any userinfo, then any port, leaving the host — the same thing
  // `urlparse().hostname` gives.
  const afterUserinfo = authority.slice(authority.lastIndexOf("@") + 1);
  const host = afterUserinfo.startsWith("[")
    ? afterUserinfo.slice(1, afterUserinfo.indexOf("]"))
    : (afterUserinfo.split(":")[0] as string);
  return { scheme: (match[1] as string).toLowerCase(), host: host.toLowerCase() };
}
