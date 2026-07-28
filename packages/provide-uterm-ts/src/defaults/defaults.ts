//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Default host/port constants for provide-uterm transports.
 *
 * Port of the Python module `provide.uterm.defaults`
 * (`packages/provide-uterm/src/provide/uterm/defaults.py`, class
 * `TerminalDefaults`) and the Go package `defaults`. Override at the call
 * site rather than shadowing these values.
 */

import { homedir } from "node:os";
import { join } from "node:path";

/** Default telnet host. */
export const TELNET_HOST = "127.0.0.1";
/** Default telnet port. */
export const TELNET_PORT = 2102;
/** Default SSH port. */
export const SSH_PORT = 2222;
/** Default gateway telnet port. */
export const GATEWAY_TELNET_PORT = 2112;
/** Default gateway SSH port. */
export const GATEWAY_SSH_PORT = 2222;

/** Bind-all address for gateway/proxy listeners. */
export const BIND_ALL = "0.0.0.0";
/** `uterm proxy` default HTTP listen port. */
export const PROXY_PORT = 8765;
/** `uterm proxy` default WebSocket path. */
export const PROXY_WS_PATH = "/ws/terminal";
/**
 * Interval in milliseconds between remote-receive polls in the WS→transport
 * proxy. Mirrors `WsTerminalProxy._POLL_MS` in Python.
 */
export const PROXY_POLL_MS = 50;
/** provide-uterm-server default bind host. */
export const SERVER_HOST = "127.0.0.1";
/** provide-uterm-server default port. */
export const SERVER_PORT = 8780;
/** Default remote telnet port (connect-to). */
export const TELNET_REMOTE_PORT = 23;
/** Default remote SSH port (connect-to). */
export const SSH_REMOTE_PORT = 22;
/** WebSocket ping interval (seconds). */
export const WS_PING_INTERVAL = 20;
/** WebSocket ping response timeout (seconds). */
export const WS_PING_TIMEOUT = 20;
/** WebSocket close timeout (seconds). */
export const WS_CLOSE_TIMEOUT = 10;
/** Reconnect attempts before giving up. */
export const RECONNECT_MAX_RETRIES = 5;
/** Initial reconnect backoff (seconds). */
export const RECONNECT_BASE_BACKOFF_S = 0.5;
/** Ceiling for reconnect backoff (seconds). */
export const RECONNECT_MAX_BACKOFF_S = 30.0;

/**
 * Default resume-token file path (`~/.uterm/session_token`).
 *
 * Port of `TerminalDefaults.token_file()`.
 */
export function tokenFile(): string {
  return join(homedir(), ".uterm", "session_token");
}
