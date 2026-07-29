//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A hosted session backed by a remote SSH shell.
 *
 * Port of `provide.uterm.server.connectors.ssh`, the third of the
 * remote-endpoint connectors and the one with a real security decision in its
 * constructor.
 *
 * **Host-key verification is on unless it is explicitly turned off.** A
 * session with no `known_hosts` is refused before anything connects, and the
 * refusal names the session, names the host, and says both ways out. The only
 * way to skip the check is to write `insecure_no_host_check` down, which is a
 * word an operator has to mean — and it still warns. Without that rule an SSH
 * connector pointed at a name would accept whatever answered, which is the
 * attack host keys exist to stop.
 */

import { MAX_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION, PREFERRED_PROTOCOL_VERSION } from "../bridge/index.ts";
import { SSH_REMOTE_PORT, TELNET_HOST } from "../defaults/index.ts";
import { assertIpAllowed } from "../egress/index.ts";
import { pyInt } from "../pycompat/index.ts";
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
  pyTruthy,
  rejectUnknownConnectorKeys,
} from "./overlay.ts";

/** The screen the shell is given, and the overlay is drawn for. */
export const SSH_COLS = OVERLAY_COLS;
export const SSH_ROWS = OVERLAY_ROWS;

/** How much is read at a time. */
const READ_SIZE = 4096;

/** The user a session connects as when it names nobody. */
const DEFAULT_USERNAME = "guest";

/** The settings this connector will take. */
export const SSH_CONNECTOR_KEYS: ReadonlySet<string> = new Set([
  "host",
  "port",
  "username",
  "password",
  "client_keys",
  "client_key_path",
  "client_key",
  "client_key_data",
  "known_hosts",
  "insecure_no_host_check",
  "input_mode",
  "hub_overlay",
  "block_private_connector_targets",
]);

/** Nothing arrived before the read gave up, which is not a disconnection. */
export class SshReadTimeout extends Error {}

/** A chunk off the shell: bytes, or text something has already decoded. */
export type SshChunk = { binary: true; data: Uint8Array } | { binary: false; data: string };

/** What the connector needs of an SSH connection, which is all it uses. */
export interface SshConnectorSession {
  /**
   * The next chunk of output.
   *
   * @throws {SshReadTimeout} When nothing arrived in time.
   */
  read(size: number): Promise<SshChunk>;
  write(data: Uint8Array): Promise<void>;
  /** Tell the shell there is no more input. */
  writeEof(): Promise<void>;
  /** Close the shell process. */
  closeProcess(): Promise<void>;
  /** Close the connection and wait for it to go. */
  closeConnection(): Promise<void>;
  /** The address actually reached, or nothing when it could not be determined. */
  peerIp(): string | undefined;
}

/** How the connection is opened, once the settings have been checked. */
export interface SshConnectOptions {
  host: string;
  port: number;
  username: string;
  password: string | undefined;
  /** A path to a known-hosts file, or nothing when the check is turned off. */
  knownHosts: string | undefined;
  /** Whatever key material the settings named, in the order they named it. */
  clientKeys: readonly string[];
  cols: number;
  rows: number;
}

/** What a caller may supply in place of the real world. */
export interface SshConnectorOptions {
  connect: (options: SshConnectOptions) => Promise<SshConnectorSession>;
  /** Seconds, as the reference's `time.time()` gives them. */
  now?: () => number;
  /** Told when a session is built with host-key checking turned off. */
  onInsecureHostCheck?: (sessionId: string, host: string) => void;
}

/**
 * Connect a hosted session to a remote SSH shell.
 *
 * @throws {Error} On construction, when the settings carry a name this
 *   connector does not have, name a key file it cannot load, or leave the host
 *   key unchecked without saying so.
 */
export class SshSessionConnector implements SessionConnector {
  readonly #sessionId: string;
  readonly #displayName: string;
  readonly #host: string;
  readonly #port: number;
  readonly #username: string;
  readonly #password: string | undefined;
  readonly #knownHosts: string | undefined;
  readonly #clientKeys: readonly string[];
  readonly #connect: (options: SshConnectOptions) => Promise<SshConnectorSession>;
  readonly #now: () => number;
  readonly #hubOverlay: boolean;
  readonly #blockPrivate: boolean;
  #session: SshConnectorSession | undefined;
  #inputMode: string;
  #paused = false;
  #receivedBytes = 0;
  #screenBuffer = "";
  #banner: string;

  constructor(
    sessionId: string,
    displayName: string,
    config: Readonly<Record<string, unknown>>,
    options: SshConnectorOptions,
  ) {
    rejectUnknownConnectorKeys("ssh", config, SSH_CONNECTOR_KEYS);
    this.#clientKeys = collectClientKeys(config);
    this.#sessionId = sessionId;
    this.#displayName = displayName;
    this.#host = String(config.host ?? TELNET_HOST);
    const port = pyInt(config.port ?? SSH_REMOTE_PORT);
    if (port === undefined) {
      throw new Error(`invalid literal for int() with base 10: '${String(config.port)}'`);
    }
    this.#port = port;
    this.#username = String(config.username ?? DEFAULT_USERNAME);
    this.#password = config.password === undefined || config.password === null ? undefined : String(config.password);
    this.#knownHosts =
      config.known_hosts === undefined || config.known_hosts === null ? undefined : String(config.known_hosts);
    if (this.#knownHosts === undefined) {
      // Read Python's way on purpose: an empty list is false there and true
      // here, and reading this flag with `Boolean()` would turn
      // `insecure_no_host_check = []` into a switched-off host-key check.
      if (!pyTruthy(config.insecure_no_host_check)) {
        throw new Error(
          `ssh_connector requires known_hosts for session '${sessionId}' connecting to ${this.#host}; ` +
            "set connector_config.known_hosts to a known_hosts file path, " +
            "or set insecure_no_host_check=true to disable host key verification",
        );
      }
      // Said out loud even though it was asked for: a session that trusts
      // whatever answers should be visible in the log of a server that starts.
      options.onInsecureHostCheck?.(sessionId, this.#host);
    }
    this.#connect = options.connect;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#inputMode = String(config.input_mode ?? "open");
    this.#hubOverlay = config.hub_overlay === undefined ? true : pyTruthy(config.hub_overlay);
    this.#blockPrivate = pyTruthy(config.block_private_connector_targets);
    this.#banner = `Connected to ssh://${this.#username}@${this.#host}:${this.#port}`;
  }

  /** The key material the settings named, for a caller to make sense of. */
  get clientKeys(): readonly string[] {
    return this.#clientKeys;
  }

  #snapshot(): WorkerMessage {
    const screen = overlayScreen({
      sessionId: this.#sessionId,
      displayName: this.#displayName,
      upstream: `ssh://${this.#username}@${this.#host}:${this.#port}`,
      inputMode: this.#inputMode,
      paused: this.#paused,
      banner: this.#banner,
      buffer: this.#screenBuffer,
      hubOverlay: this.#hubOverlay,
    });
    return overlaySnapshot(screen, "ssh_stream", this.#now());
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
   * Open the connection, check where it went, and only then ask for a shell.
   *
   * The handshake — host key included — completed against the name, so this
   * touches no verification; it reads where the connection actually landed,
   * before any application data. A blocked address is closed on and nothing is
   * ever typed into it.
   *
   * @throws {EgressBlockedError} On an address the policy refuses.
   */
  async start(): Promise<void> {
    const session = await this.#connect({
      host: this.#host,
      port: this.#port,
      username: this.#username,
      password: this.#password,
      knownHosts: this.#knownHosts,
      clientKeys: this.#clientKeys,
      cols: SSH_COLS,
      rows: SSH_ROWS,
    });
    const peerIp = session.peerIp();
    if (peerIp !== undefined && peerIp !== "") {
      try {
        assertIpAllowed(peerIp, { blockPrivate: this.#blockPrivate });
      } catch (error) {
        await session.closeConnection().catch(() => undefined);
        throw error;
      }
    }
    this.#session = session;
  }

  /**
   * Tear the session down: the shell first, then the process, then the
   * connection.
   *
   * In that order, so the far end sees an ended input stream rather than a
   * dropped connection — the difference between a shell that exits and one
   * that is killed.
   */
  async stop(): Promise<void> {
    const session = this.#session;
    this.#session = undefined;
    if (session === undefined) {
      return;
    }
    await session.writeEof().catch(() => undefined);
    await session.closeProcess().catch(() => undefined);
    await session.closeConnection().catch(() => undefined);
  }

  isConnected(): boolean {
    return this.#session !== undefined;
  }

  /**
   * Whatever the shell has written, as terminal output and a new snapshot.
   *
   * Nothing to read is not a disconnection — it is what a shell waiting at a
   * prompt looks like — so a read that times out says nothing at all.
   */
  async pollMessages(): Promise<WorkerMessage[]> {
    const session = this.#session;
    if (session === undefined) {
      return [];
    }
    let chunk: SshChunk;
    try {
      chunk = await session.read(READ_SIZE);
    } catch (error) {
      if (error instanceof SshReadTimeout) {
        return [];
      }
      throw error;
    }
    // Text is put back into bytes before it is read as CP437, which is what
    // the reference does — so a character latin-1 cannot hold becomes `?`
    // rather than passing through.
    const payload = chunk.binary ? chunk.data : latin1Bytes(chunk.data);
    if (payload.length === 0) {
      return [];
    }
    this.#receivedBytes += payload.length;
    const text = decodeCp437(payload);
    this.#screenBuffer = boundedBuffer(this.#screenBuffer, text);
    this.#banner = `Received ${this.#receivedBytes} bytes from SSH upstream.`;
    return [{ type: "term", data: text, ts: this.#now() }, this.#snapshot()];
  }

  async handleInput(data: string): Promise<WorkerMessage[]> {
    const session = this.#session;
    if (session !== undefined) {
      // Sent as UTF-8, unlike telnet's CP437: a modern shell reads UTF-8, and
      // this connector's own overlay is the only thing that speaks CP437.
      await session.write(new TextEncoder().encode(data));
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
      `[ssh session analysis — worker: ${this.#sessionId}]`,
      `host: ${this.#host}`,
      `port: ${this.#port}`,
      `user: ${this.#username}`,
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
 * The key material a session named, in the order it named it.
 *
 * `client_key_path` is refused rather than ignored: a session naming a key
 * file and silently connecting without it would look like it worked until the
 * far end asked for the key.
 *
 * @throws {Error} When the settings name a key path.
 */
function collectClientKeys(config: Readonly<Record<string, unknown>>): string[] {
  const keys: string[] = [];
  const named = config.client_keys;
  if (named !== undefined && named !== null) {
    if (Array.isArray(named)) {
      // A hole in the list is skipped rather than passed on as nothing.
      keys.push(...named.filter((key) => key !== null && key !== undefined).map((key) => String(key)));
    } else {
      keys.push(String(named));
    }
  }
  if (config.client_key_path !== undefined && config.client_key_path !== null) {
    throw new Error("ssh connector_config.client_key_path is not supported");
  }
  if (config.client_key !== undefined && config.client_key !== null) {
    keys.push(String(config.client_key));
  }
  if (config.client_key_data !== undefined && config.client_key_data !== null) {
    keys.push(String(config.client_key_data));
  }
  return keys;
}

/** Text back into bytes the way `str.encode("latin-1", errors="replace")` does. */
function latin1Bytes(text: string): Uint8Array {
  return Uint8Array.from(
    [...text].map((character) => {
      const code = character.codePointAt(0) as number;
      // A character latin-1 cannot hold becomes `?`, as the reference's
      // `errors="replace"` makes it.
      return code > 0xff ? 0x3f : code;
    }),
  );
}
