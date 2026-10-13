//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Public connection events for the terminal elements (`<uterm-session>` and
 * `<uterm-terminal>`), so an embedding page can follow the stream without
 * reading private state.
 *
 * - `uterm:connection` (`detail: {connected}`) fires when the element's socket
 *   opens and when an open socket goes away (a close, `disconnect()`, or a
 *   `connect()` that replaces it). Changes only: never twice in a row with the
 *   same value.
 * - `uterm:close` (`detail: {code, reason, wasClean, willReconnect}`) fires for
 *   every socket the server closes or that drops, opened or not, before the
 *   element schedules its own reconnect. It is cancelable: `preventDefault()`
 *   stops that reconnect. It does not fire for a close the page asked for with
 *   `disconnect()`, nor for a socket replaced by `connect()`.
 *
 * All events bubble and are composed, so a listener on the element (or any
 * ancestor, across shadow roots) hears them. Every socket the element opens
 * fires them, its own reconnects included.
 */

export const UTERM_CONNECTION_EVENT = "uterm:connection";
export const UTERM_CLOSE_EVENT = "uterm:close";

/**
 * The server revoked this viewer's access (`POST /api/sessions/{id}/viewers/revoke`).
 * Echoes HTTP 410 Gone: the credential the element holds is refused from now on,
 * so a reconnect could only fail.
 */
export const VIEWER_REVOKED_CLOSE_CODE = 4410;

/**
 * Close codes after which the element does not reconnect by itself, listener or
 * not. Kept deliberately small: only codes that say the element's credential is
 * refused for good. Other policy closes (e.g. 4403 cross-origin, 1008) keep the
 * usual reconnect with backoff, as before; a page that knows better can cancel
 * `uterm:close`.
 */
export const FINAL_CLOSE_CODES: ReadonlySet<number> = new Set([VIEWER_REVOKED_CLOSE_CODE]);

export function isFinalCloseCode(code: number): boolean {
  return FINAL_CLOSE_CODES.has(code);
}

export interface UtermConnectionEventDetail {
  /** True once the socket is open; false once it is gone. */
  connected: boolean;
}

export interface UtermCloseEventDetail {
  /** The WebSocket close code (1006 for a drop with no close frame). */
  code: number;
  /** The close reason the server sent, or "". */
  reason: string;
  wasClean: boolean;
  /** Whether the element will reconnect by itself if no listener cancels the event. */
  willReconnect: boolean;
}

/** What the socket's close event said. */
export interface SocketCloseInfo {
  code: number;
  reason: string;
  wasClean: boolean;
}

export function closeInfo(event: CloseEvent | undefined): SocketCloseInfo {
  return {
    code: typeof event?.code === "number" ? event.code : 1006,
    reason: typeof event?.reason === "string" ? event.reason : "",
    wasClean: event?.wasClean === true,
  };
}

/**
 * Tracks one element's connection state and dispatches its public events.
 * The element calls `opened()` from its socket's open handler, `dropped()` when
 * it lets a socket go itself, and `closed()` from the socket's close handler.
 */
export class ConnectionEvents {
  private _connected = false;

  constructor(private readonly target: EventTarget) {}

  get connected(): boolean {
    return this._connected;
  }

  opened(): void {
    this._set(true);
  }

  dropped(): void {
    this._set(false);
  }

  /**
   * A socket closed on its own (server close or drop). Reports the drop, then
   * the close, and returns whether the element should schedule its reconnect.
   * `reconnecting` is whether the element would reconnect at all.
   */
  closed(info: SocketCloseInfo, reconnecting = true): boolean {
    this._set(false);
    const willReconnect = reconnecting && !isFinalCloseCode(info.code);
    const event = new CustomEvent<UtermCloseEventDetail>(UTERM_CLOSE_EVENT, {
      bubbles: true,
      composed: true,
      cancelable: true,
      detail: { ...info, willReconnect },
    });
    const notCancelled = this.target.dispatchEvent(event);
    return willReconnect && notCancelled;
  }

  private _set(connected: boolean): void {
    if (this._connected === connected) return;
    this._connected = connected;
    this.target.dispatchEvent(
      new CustomEvent<UtermConnectionEventDetail>(UTERM_CONNECTION_EVENT, {
        bubbles: true,
        composed: true,
        detail: { connected },
      }),
    );
  }
}

declare global {
  interface HTMLElementEventMap {
    "uterm:connection": CustomEvent<UtermConnectionEventDetail>;
    "uterm:close": CustomEvent<UtermCloseEventDetail>;
  }
}
