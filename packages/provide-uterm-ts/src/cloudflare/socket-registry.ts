//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Which connections a session is holding.
 *
 * Port of the registry half of
 * `provide.uterm.cloudflare.do.session_runtime.ws_helpers`.
 *
 * A Durable Object is evicted and resumed with its sockets still open, so a
 * connection's identity has to survive being handed back by the runtime — the
 * key is stamped on the socket itself rather than derived from anything about
 * it.
 */

import { randomBytes } from "node:crypto";

/** Where the key is stamped, so it survives the runtime handing the socket back. */
const KEY_FIELD = "_ut_ws_key";

/** How many random bytes go in a key, alongside the timestamp. */
const KEY_ENTROPY_BYTES = 4;

/** A connection, as much of one as the registry touches. */
export interface RegistrySocket {
  [KEY_FIELD]?: string;
}

/** What has to be let go when a connection ends. */
export interface ConnectionState {
  forget(wsId: string): void;
}

/**
 * The identity of a connection, stable for its lifetime.
 *
 * Stamped on the socket on first use. Nothing about a connection is unique
 * enough to derive one from — two browsers behind one address are two
 * connections — so it is generated and remembered.
 */
export function wsKey(socket: RegistrySocket, now: () => bigint = process.hrtime.bigint): string {
  const existing = socket[KEY_FIELD];
  if (typeof existing === "string" && existing !== "") {
    return existing;
  }
  const key = `${now()}_${randomBytes(KEY_ENTROPY_BYTES).toString("hex")}`;
  try {
    socket[KEY_FIELD] = key;
  } catch {
    // A frozen socket cannot carry its key, so it gets a fresh one each time.
    // Worse than remembering it, and better than failing to register it.
  }
  return key;
}

/**
 * The connections one session is holding.
 *
 * A session has exactly one worker at a time and any number of browsers and
 * raw connections, which is why the worker is a slot and the others are maps.
 */
export class SocketRegistry {
  /** The worker, if one is attached. */
  worker: RegistrySocket | undefined;
  readonly browsers = new Map<string, RegistrySocket>();
  readonly raw = new Map<string, RegistrySocket>();
  /** Which browser owns the hijack, if any. */
  readonly hijackOwners = new Map<string, string>();
  /** The resume token each browser was issued. */
  readonly resumeTokens = new Map<string, string>();
  readonly #flow: ConnectionState;

  constructor(flow: ConnectionState) {
    this.#flow = flow;
  }

  /**
   * Take a connection into the registry.
   *
   * Registering a second worker replaces the first: a session has one, so
   * accumulating them would leave output going to a socket nobody reads.
   * Anything that is not a worker or a raw connection is a browser — the
   * overwhelming majority, and the safest thing to be mistaken for.
   */
  register(socket: RegistrySocket, role: string): void {
    if (role === "worker") {
      this.worker = socket;
      return;
    }
    (role === "raw" ? this.raw : this.browsers).set(wsKey(socket), socket);
  }

  /**
   * Let a connection go, and everything keyed by it.
   *
   * Not merely the registry: the hijack it owned, its resume token and its
   * flow-control accounting go too. Anything left behind is state for a
   * connection that no longer exists — and the flow controller in particular
   * would keep counting a browser that can never acknowledge again, which is
   * what stalls the producer for every other viewer.
   */
  remove(socket: RegistrySocket): void {
    const wsId = wsKey(socket);
    // By identity, not by id: a browser disconnecting must not detach the
    // worker from the session.
    if (socket === this.worker) {
      this.worker = undefined;
    }
    this.browsers.delete(wsId);
    this.raw.delete(wsId);
    this.hijackOwners.delete(wsId);
    this.resumeTokens.delete(wsId);
    this.#flow.forget(wsId);
  }

  /**
   * The browsers currently connected, excluding one.
   *
   * The exclusion is how a joining browser is told about its peers rather
   * than about itself: it is already registered by the time the runtime
   * reports the connection open.
   */
  presenceIds(exclude?: RegistrySocket): string[] {
    const excluded = exclude === undefined ? undefined : wsKey(exclude);
    return [...this.browsers.keys()].filter((key) => key !== excluded);
  }
}
