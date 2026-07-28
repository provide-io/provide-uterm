//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The interface every wire protocol implements.
 *
 * Port of the Python module `provide.uterm.transports.base`.
 *
 * Telnet, SSH, WebSocket and the chaos wrapper are interchangeable to a
 * caller, which is what lets a session be pointed at any of them.
 */

/** Raised when a transport is not connected, or the connection is lost. */
export class TransportConnectionError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "TransportConnectionError";
  }
}

/** A wire protocol a session can read from and write to. */
export interface ConnectionTransport {
  /**
   * Establish a connection to a remote host.
   *
   * @throws {TransportConnectionError} If the connection cannot be made.
   */
  connect(host: string, port: number, options?: Record<string, unknown>): Promise<void>;

  /** Close the connection and release resources. Safe to call twice. */
  disconnect(): Promise<void>;

  /**
   * Send raw bytes, with whatever encoding or escaping the protocol needs.
   *
   * @throws {TransportConnectionError} If not connected, or the send fails.
   */
  send(data: Uint8Array): Promise<void>;

  /**
   * Read raw bytes, returning empty on a read timeout.
   *
   * @throws {TransportConnectionError} If not connected, or the connection is
   *   lost.
   */
  receive(maxBytes: number, timeoutMs: number): Promise<Uint8Array>;

  /** Whether the connection is live. */
  isConnected(): boolean;
}
