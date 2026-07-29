//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * `uterm share` — hand somebody a URL onto a live terminal.
 *
 * Port of `provide.uterm.cli.share`. The decisions made before the first byte
 * moves are the ones that matter:
 *
 * * **Where the bearer token comes from**, in order: the flag, then the named
 *   file, then the default file, then nothing.
 * * **Where the WebSocket actually is.** The server may answer with a full URL
 *   or with a path; a path is joined to the server it came from with the
 *   scheme upgraded, `http` to `ws` and `https` to `wss`. Getting that wrong
 *   is a connection that fails, or a shared terminal over cleartext.
 * * **What is printed**, since the two URLs are the whole output: one is for
 *   watching and one is for typing, and handing over the wrong one hands over
 *   control.
 */

import { CHANNEL_DATA, encodeFrame } from "../tunnel/protocol.ts";

/** What the caller asked for. */
export interface ShareArgs {
  server: string;
  token?: string | undefined;
  tokenFile?: string | undefined;
  displayName?: string | undefined;
  attach?: boolean | undefined;
  cmd?: readonly string[] | undefined;
}

/** The surroundings the command reads, injected so a test need not have them. */
export interface ShareEnvironment {
  /** The contents of a token file, or nothing when there is no file to read. */
  readTokenFile(path: string): string | undefined;
  /** Where a token lives when nobody named a file. */
  defaultTokenFile: string;
  /** Who is sharing. */
  getUser(): string;
  /** What they are sharing from. */
  hostname(): string;
}

/**
 * The bearer token, or nothing.
 *
 * A flag beats a file, which is what lets somebody override a stale
 * credential without deleting it. An *empty* flag does not: the reference
 * tests the value for truth, so `--token ""` falls through to the file.
 */
export function resolveToken(args: ShareArgs, environment: ShareEnvironment): string | undefined {
  if (args.token !== undefined && args.token !== "") {
    return args.token;
  }
  const contents = environment.readTokenFile(
    args.tokenFile !== undefined && args.tokenFile !== "" ? args.tokenFile : environment.defaultTokenFile,
  );
  // A file holding only whitespace resolves to the empty string rather than to
  // nothing — falsy either way to whoever asks, but not the same value.
  return contents === undefined ? undefined : contents.trim();
}

/** What a viewer on the other end sees this session called. */
export function resolveDisplayName(args: ShareArgs, environment: ShareEnvironment): string {
  if (args.displayName !== undefined && args.displayName !== "") {
    return args.displayName;
  }
  let user: string;
  try {
    user = environment.getUser();
  } catch {
    // A user nobody can name is still a session worth labelling.
    user = "unknown";
  }
  return `${user}@${environment.hostname() || "localhost"}`;
}

/**
 * Where the WebSocket is, given what the server answered.
 *
 * An absolute endpoint is taken as it stands. A path is joined to the server
 * it came from, with the scheme upgraded so a session shared over TLS is
 * carried over TLS.
 */
export function resolveWsEndpoint(server: string, endpoint: string): string {
  if (!endpoint.startsWith("/")) {
    return endpoint;
  }
  // The reference's two replacements, with its reach: they are unanchored, so
  // a server whose *path* contains `http://` has that rewritten too. Carried
  // over rather than corrected — the alternative is a port that resolves a URL
  // the reference does not.
  //
  // Their order does not matter, and no test can show that it does: `https://`
  // does not contain `http://`, so neither replacement can create or destroy a
  // match for the other.
  const base = server.replace(/\/+$/, "").replaceAll("http://", "ws://").replaceAll("https://", "wss://");
  return `${base}${endpoint}`;
}

/** What the command decided to do. */
export type SharePlan =
  | {
      ok: true;
      /** Lines written to standard output, in order. */
      output: string[];
      wsEndpoint: string;
      workerToken: string;
      displayName: string;
      token: string | undefined;
      /** Whether this attaches to the caller's terminal rather than spawning. */
      attach: boolean;
      cmd: readonly string[] | undefined;
    }
  | { ok: false; error: string; exitCode: number };

/** What the tunnel server answered when asked for a tunnel. */
export interface TunnelInfo {
  share_url?: unknown;
  control_url?: unknown;
  ws_endpoint?: unknown;
  worker_token?: unknown;
}

/**
 * Work out what sharing this session would do.
 *
 * Separated from doing it so the decisions can be checked without a terminal,
 * a network or a process.
 */
export function planShare(args: ShareArgs, info: TunnelInfo, environment: ShareEnvironment): SharePlan {
  const shareUrl = typeof info.share_url === "string" ? info.share_url : "";
  const controlUrl = typeof info.control_url === "string" ? info.control_url : "";
  const endpoint = typeof info.ws_endpoint === "string" ? info.ws_endpoint : "";
  const workerToken = typeof info.worker_token === "string" ? info.worker_token : "";

  if (endpoint === "") {
    // Nothing to connect to, and printing the URLs first would advertise a
    // share that was never established.
    return { ok: false, error: "error: server response missing ws_endpoint", exitCode: 1 };
  }

  return {
    ok: true,
    output: [
      "Sharing terminal session...",
      `  View:    ${shareUrl}`,
      `  Control: ${controlUrl}`,
      "",
      "Connected. Press Ctrl+C to stop sharing.",
    ],
    wsEndpoint: resolveWsEndpoint(args.server, endpoint),
    workerToken,
    displayName: resolveDisplayName(args, environment),
    token: resolveToken(args, environment),
    attach: args.attach === true,
    cmd: args.cmd,
  };
}

/** The terminal side of the bridge. */
export interface BridgeSource {
  read(size: number): Promise<Uint8Array>;
  write(data: Uint8Array): Promise<void>;
  /** Where received bytes go when attached to the caller's own terminal. */
  writeLocal(data: Uint8Array): Promise<void>;
}

/** How many bytes are asked for at a time. */
export const READ_SIZE = 4096;

/** Whether a failure is one side hanging up rather than a fault worth raising. */
function isHangUp(error: unknown): boolean {
  if (!(error instanceof Error)) {
    return false;
  }
  // The reference catches `OSError` and `EOFError` — a socket or a pty going
  // away. Anything else is a bug and is left to escape.
  return error.name === "EOFError" || typeof (error as { code?: unknown }).code === "string";
}

/**
 * Carry bytes both ways until either side closes.
 *
 * Both directions run at once and the bridge ends when both have finished, so
 * a terminal that stops producing does not strand input still arriving from
 * the far end.
 */
export async function bridgeLoop(
  source: BridgeSource,
  wsSend: (frame: Uint8Array) => Promise<void>,
  wsRecv: () => Promise<Uint8Array>,
  options: { isAttach?: boolean } = {},
): Promise<void> {
  const isAttach = options.isAttach === true;

  const terminalToSocket = async (): Promise<void> => {
    try {
      for (;;) {
        const data = await source.read(READ_SIZE);
        if (data.length === 0) {
          return;
        }
        await wsSend(encodeFrame(CHANNEL_DATA, data));
      }
    } catch (error) {
      if (!isHangUp(error)) {
        throw error;
      }
    }
  };

  const socketToTerminal = async (): Promise<void> => {
    try {
      for (;;) {
        const data = await wsRecv();
        if (data.length === 0) {
          return;
        }
        // Attached, the caller's own terminal is the destination; otherwise it
        // is the pty this command spawned.
        await (isAttach ? source.writeLocal(data) : source.write(data));
      }
    } catch (error) {
      if (!isHangUp(error)) {
        throw error;
      }
    }
  };

  await Promise.all([terminalToSocket(), socketToTerminal()]);
}
