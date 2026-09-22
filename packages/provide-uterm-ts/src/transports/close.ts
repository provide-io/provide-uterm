//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * How a transport connection ended.
 *
 * Port of the Python module `provide.uterm.transport_close`.
 *
 * Every transport reports the end of its connection as a
 * {@link TransportClosedError} carrying a {@link TransportClose}: which side
 * ended it, plus the protocol's close code and reason when it has them. The
 * transport session keeps the close it observed, so a caller can tell a
 * client-side keepalive timeout from a server closing the socket.
 */

import { TransportConnectionError } from "./base.ts";

/** Which side ended a connection. */
export type CloseInitiator = "local" | "remote" | "unknown";

/** The optional parts of a {@link TransportClose}. */
export interface TransportCloseFields {
  /** Protocol close code, when the protocol has one (WebSocket). */
  code?: number | undefined;
  /** Protocol close reason, when one was given. */
  reason?: string | undefined;
  /** Transport-specific description, such as the underlying error. */
  detail?: string | undefined;
}

/** Why a transport connection ended. */
export class TransportClose {
  /** The side that ended the connection. */
  readonly initiator: CloseInitiator;
  /** Protocol close code, when the protocol has one. */
  readonly code: number | undefined;
  /** Protocol close reason; empty when none was given. */
  readonly reason: string;
  /** Transport-specific description; empty when there is none. */
  readonly detail: string;

  constructor(initiator: CloseInitiator, fields: TransportCloseFields = {}) {
    this.initiator = initiator;
    this.code = fields.code;
    this.reason = fields.reason ?? "";
    this.detail = fields.detail ?? "";
  }

  /**
   * One line: the initiator, then code and reason, then any detail.
   *
   * Formatted exactly as the reference does it, because the summary is folded
   * into the error message and the message is what the parity corpora record.
   */
  summary(): string {
    const parts = [`${this.initiator} close`];
    if (this.code !== undefined) {
      parts.push(String(this.code));
    }
    if (this.reason !== "") {
      parts.push(this.reason);
    }
    const text = parts.join(" ");
    return this.detail === "" ? text : `${text} (${this.detail})`;
  }
}

/**
 * Raised by a transport when its connection has ended.
 *
 * A {@link TransportConnectionError}, so a caller matching the old untyped
 * error still catches it; the message keeps the old prefix, with the close
 * summary appended, so a caller matching the old message still matches.
 */
export class TransportClosedError extends TransportConnectionError {
  /** How the connection ended. */
  readonly close: TransportClose;

  constructor(message: string, close: TransportClose, options?: { cause?: unknown }) {
    super(`${message} (${close.summary()})`, options);
    this.name = "TransportClosedError";
    this.close = close;
  }
}

/**
 * Describe a connection that ended with `error`, attributing it to
 * `initiator`.
 *
 * The detail is `"<ErrorName>: <message>"`, the shape the reference gives an
 * exception; a thrown value that is not an `Error` is described as it prints.
 */
export function closeFromException(error: unknown, initiator: CloseInitiator = "unknown"): TransportClose {
  const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  return new TransportClose(initiator, { detail });
}
