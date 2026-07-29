//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The two ways a caller opens a terminal session.
 *
 * Port of `provide.uterm.telnet_session.connect_telnet` and
 * `provide.uterm.ws_session.connect_ws` — the functions a caller actually
 * reaches for, as distinct from the session classes they return.
 *
 * Each decides two things. The defaults a session gets when nobody says
 * otherwise are the shape a BBS expects — eighty by twenty-five, `ANSI`, CP437
 * on the way in — and not what a modern terminal library would pick on its
 * own. And what reaches the transport: the WebSocket factory *omits* an origin
 * and headers it was not given rather than passing them as nothing, which is
 * what lets a worker gating cross-origin upgrades see a real Origin instead of
 * a null one.
 *
 * Opening the connection is the caller's, so the transport arrives built. That
 * keeps this module free of any socket and lets the same code run in a browser,
 * a Worker or Node.
 */

import {
  TELNET_SESSION_DEFAULTS,
  TelnetSession,
  type TelnetSessionOptions,
  WEBSOCKET_SESSION_DEFAULTS,
  WebSocketSession,
  type WebSocketSessionOptions,
} from "./adapters.ts";
import type { SessionTransport } from "./transport-session.ts";

/** What a telnet transport is told to open. */
export interface TelnetConnectArgs {
  host: string;
  port: number;
  /** Advertised over NAWS, so the far end wraps where this session wraps. */
  cols: number;
  rows: number;
  /** Advertised over TTYPE. */
  term: string;
  /** Seconds. */
  timeout: number;
}

/** What a WebSocket transport is told to open. */
export interface WsConnectArgs {
  url: string;
  /** Seconds. */
  pingInterval: number;
  pingTimeout: number;
  closeTimeout: number;
  /**
   * Present only when the caller gave one.
   *
   * A worker that gates cross-origin upgrades reads this; sending it as
   * nothing is not the same as not sending it, and is refused.
   */
  origin?: string;
  /** Present only when the caller gave them. */
  additionalHeaders?: Record<string, string>;
}

/** What a caller may say about a telnet session. */
export interface ConnectTelnetOptions extends Omit<TelnetSessionOptions, "transport" | "host" | "port"> {
  /** Opens the connection. Given the arguments the session settled on. */
  open: (args: TelnetConnectArgs) => Promise<SessionTransport>;
}

/** What a caller may say about a WebSocket session. */
export interface ConnectWsOptions extends Omit<WebSocketSessionOptions, "transport" | "url"> {
  /** Opens the connection. Given the arguments the session settled on. */
  open: (args: WsConnectArgs) => Promise<SessionTransport>;
  /** Seconds between pings. */
  pingInterval?: number;
  pingTimeout?: number;
  closeTimeout?: number;
  /** Sent only when given — see {@link WsConnectArgs.origin}. */
  origin?: string;
  /** Sent only when given. */
  additionalHeaders?: Record<string, string>;
}

/** How long a WebSocket session waits, in seconds, when nobody says. */
export const WS_PING_INTERVAL_S = 20;
export const WS_PING_TIMEOUT_S = 20;
export const WS_CLOSE_TIMEOUT_S = 10;

/**
 * Open a telnet session and connect it.
 *
 * The screen size reaches the far end over NAWS and the terminal type over
 * TTYPE, so a BBS draws for the screen this session actually has.
 */
export async function connectTelnet(host: string, port: number, options: ConnectTelnetOptions): Promise<TelnetSession> {
  const cols = options.cols ?? TELNET_SESSION_DEFAULTS.cols;
  const rows = options.rows ?? TELNET_SESSION_DEFAULTS.rows;
  const term = options.term ?? TELNET_SESSION_DEFAULTS.term;
  const timeout = options.connectTimeoutS ?? TELNET_SESSION_DEFAULTS.connectTimeoutS;
  const transport = await options.open({ host, port, cols, rows, term, timeout });
  const session = new TelnetSession({
    ...options,
    transport,
    host,
    port,
    cols,
    rows,
    term,
    connectTimeoutS: timeout,
  });
  await session.connect();
  return session;
}

/**
 * Open a WebSocket session and connect it.
 *
 * An origin or headers the caller did not give are left out of the call
 * entirely rather than passed as nothing.
 */
export async function connectWs(url: string, options: ConnectWsOptions): Promise<WebSocketSession> {
  const args: WsConnectArgs = {
    url,
    pingInterval: options.pingInterval ?? WS_PING_INTERVAL_S,
    pingTimeout: options.pingTimeout ?? WS_PING_TIMEOUT_S,
    closeTimeout: options.closeTimeout ?? WS_CLOSE_TIMEOUT_S,
    ...(options.origin === undefined ? {} : { origin: options.origin }),
    ...(options.additionalHeaders === undefined ? {} : { additionalHeaders: options.additionalHeaders }),
  };
  const transport = await options.open(args);
  const session = new WebSocketSession({
    ...options,
    transport,
    url,
    cols: options.cols ?? WEBSOCKET_SESSION_DEFAULTS.cols,
    rows: options.rows ?? WEBSOCKET_SESSION_DEFAULTS.rows,
  });
  await session.connect();
  return session;
}
