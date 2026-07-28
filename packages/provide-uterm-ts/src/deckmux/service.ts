//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Presence routing and control transfer for a session.
 *
 * Port of the Python module `provide.uterm.deckmux._service`.
 *
 * This is the part several browsers talk to at once, so what it broadcasts —
 * and what it declines to broadcast — is what everybody else sees. It owns the
 * per-session presence store and transfer manager, and routes the three
 * messages a browser can send plus the connect and disconnect hooks.
 */

import { randomUUID } from "node:crypto";
import { generateColor, generateInitials, generateName } from "./names.ts";
import { PresenceStore, presenceToWire, type UserPresence } from "./presence.ts";
import {
  type KeystrokeQueueMode,
  MSG_CONTROL_REQUEST,
  MSG_PRESENCE_UPDATE,
  MSG_QUEUED_INPUT,
  makeControlTransfer,
  makePresenceLeave,
} from "./protocol.ts";
import { DEFAULT_AUTO_TRANSFER_IDLE_S, TransferManager } from "./transfer.ts";

/** What the service needs of its host. */
export interface DeckMuxHub {
  broadcast(workerId: string, message: Record<string, unknown>): Promise<void>;
}

/** A browser connection, used only as an identity. */
export type BrowserSocket = object;

/** An authenticated principal, as the hub hands it over. */
export interface DeckMuxPrincipal {
  subjectId?: string;
  displayName?: string;
}

/** Settings a session's transfer manager is built with. */
export interface DeckMuxConfig {
  auto_transfer_idle_s?: number;
  keystroke_queue?: string;
}

/** Options for {@link DeckMuxPresence}. */
export interface DeckMuxPresenceOptions {
  /** Where a fresh anonymous identity comes from. */
  newUserId?: () => string;
  /** Wall clock in seconds, for the presence stores. */
  now?: () => number;
}

/** How long a participant may be silent before a joiner prunes them. */
const RECONNECT_DEBRIS_S = 30.0;

/** The settings a joiner is told about. */
const SYNC_CONFIG: Readonly<Record<string, unknown>> = {
  auto_transfer_idle_s: 30,
  keystroke_queue: "display",
};

/** The presence fields a browser is allowed to set. */
const BROWSER_FIELDS: readonly string[] = [
  "scroll_line",
  "scroll_range",
  "total_lines",
  "selection",
  "pin",
  "typing",
  "cols",
  "rows",
];

/** Presence and control transfer for every session on one hub. */
export class DeckMuxPresence {
  readonly #hub: DeckMuxHub;
  readonly #newUserId: () => string;
  readonly #now: (() => number) | undefined;
  readonly #stores = new Map<string, PresenceStore>();
  readonly #managers = new Map<string, TransferManager>();
  /**
   * The identity minted for each live connection.
   *
   * A WeakMap rather than a property on the connection: it works whatever the
   * socket is, and it lets go when the connection does.
   */
  readonly #identities = new WeakMap<object, string>();

  constructor(hub: DeckMuxHub, options: DeckMuxPresenceOptions = {}) {
    this.#hub = hub;
    this.#newUserId = options.newUserId ?? (() => randomUUID().replaceAll("-", ""));
    this.#now = options.now;
  }

  /**
   * A stable identity for one connection.
   *
   * Minted, not derived from the object. The reference is explicit about why:
   * an address is reused once the object it belonged to is collected, so a
   * browser connecting now could be handed the presence — and the ownership —
   * of one that disconnected a moment ago.
   */
  #anonymousId(socket: BrowserSocket): string {
    const existing = this.#identities.get(socket);
    if (existing !== undefined) {
      return existing;
    }
    const minted = this.#newUserId();
    this.#identities.set(socket, minted);
    return minted;
  }

  /** Who a message or a connection is from. */
  #userId(socket: BrowserSocket, principal?: DeckMuxPrincipal): string {
    // Resolved the same way everywhere, so an update finds the participant
    // the connect added.
    return principal?.subjectId ?? this.#anonymousId(socket);
  }

  /** The presence store for a session, made if it is new. */
  getPresenceStore(workerId: string): PresenceStore {
    const existing = this.#stores.get(workerId);
    if (existing !== undefined) {
      return existing;
    }
    const store = new PresenceStore(this.#now === undefined ? {} : { now: this.#now });
    this.#stores.set(workerId, store);
    return store;
  }

  /**
   * The transfer manager for a session, made if it is new.
   *
   * The settings apply only when it is made. Rebuilding it mid-session would
   * drop the queue and the warning state along with it, losing what somebody
   * had typed while they waited.
   */
  getTransferManager(workerId: string, config?: DeckMuxConfig): TransferManager {
    const existing = this.#managers.get(workerId);
    if (existing !== undefined) {
      return existing;
    }
    const manager = new TransferManager({
      autoTransferIdleS: config?.auto_transfer_idle_s ?? DEFAULT_AUTO_TRANSFER_IDLE_S,
      keystrokeQueueMode: (config?.keystroke_queue ?? "display") as KeystrokeQueueMode,
    });
    this.#managers.set(workerId, manager);
    return manager;
  }

  /**
   * Seat a browser and tell it who else is here.
   *
   * The sync goes back to the joiner always, and out to everybody else only
   * once there is somebody else to tell — a broadcast into an empty room is a
   * wasted round trip on every connect. The whole sync goes out rather than
   * just the new participant, because the others have to add them to their
   * own lists.
   */
  async onBrowserConnect(
    workerId: string,
    socket: BrowserSocket,
    role: string,
    principal?: DeckMuxPrincipal,
  ): Promise<Record<string, unknown>> {
    const store = this.getPresenceStore(workerId);
    const userId = this.#userId(socket, principal);
    // An authenticated participant is named by their claim, and falls back to
    // their subject rather than to a generated name: a real identity should
    // not be displayed as an invented one.
    const name =
      principal?.subjectId === undefined
        ? generateName(userId)
        : principal.displayName !== undefined && principal.displayName !== ""
          ? principal.displayName
          : userId;

    const color = generateColor(userId, store.takenColors());
    // Reconnect debris: a browser that dropped without a close frame is still
    // in the store, holding a colour and a slot in everybody's list. Swept
    // before the add, as the reference does — though the joiner was stamped a
    // moment ago and so is never idle by their own clock, which means the
    // sweep either side of the add reaches the same room.
    store.pruneIdle(RECONNECT_DEBRIS_S);
    store.add(userId, name, color, role, generateInitials(name));

    const sync = store.getSyncPayload({ ...SYNC_CONFIG });
    if (store.count > 1) {
      await this.#hub.broadcast(workerId, sync);
    }
    return sync;
  }

  /**
   * Unseat a browser and tell the others.
   *
   * Silent when there was nobody to remove: a second disconnect for one
   * connection must not announce that somebody already gone has left again.
   */
  async onBrowserDisconnect(workerId: string, socket: BrowserSocket, principal?: DeckMuxPrincipal): Promise<void> {
    const store = this.getPresenceStore(workerId);
    const userId = this.#userId(socket, principal);
    if (store.remove(userId) !== undefined) {
      await this.#hub.broadcast(workerId, makePresenceLeave(userId));
    }
  }

  /** Route a message from a browser. */
  async handleMessage(
    workerId: string,
    socket: BrowserSocket,
    message: Record<string, unknown>,
    principal?: DeckMuxPrincipal,
  ): Promise<void> {
    const userId = this.#userId(socket, principal);
    if (message.type === MSG_PRESENCE_UPDATE) {
      await this.#onPresenceUpdate(workerId, userId, message);
    } else if (message.type === MSG_QUEUED_INPUT) {
      await this.#onQueuedInput(workerId, userId, message);
    } else if (message.type === MSG_CONTROL_REQUEST) {
      await this.#onControlRequest(workerId, userId);
    }
    // Anything else is not ours to route.
  }

  /** Broadcast somebody's whole record, tagged as an update. */
  async #broadcastPresence(workerId: string, presence: UserPresence): Promise<void> {
    // The whole record rather than the delta: a browser that missed an
    // earlier update would otherwise render a participant with holes in them.
    await this.#hub.broadcast(workerId, { ...presenceToWire(presence), type: MSG_PRESENCE_UPDATE });
  }

  /** Apply what a browser says about itself. */
  async #onPresenceUpdate(workerId: string, userId: string, message: Record<string, unknown>): Promise<void> {
    const store = this.getPresenceStore(workerId);
    // Filtered to the allow-list rather than refused, so a field from a newer
    // frontend costs the update nothing.
    const fields: Record<string, unknown> = {};
    for (const key of BROWSER_FIELDS) {
      if (Object.hasOwn(message, key)) {
        fields[key] = message[key];
      }
    }

    let user: UserPresence | undefined;
    try {
      user = store.update(userId, fields);
    } catch {
      // An oversized selection or pin. Swallowed deliberately: without this a
      // browser could end everybody else's session by sending one message.
      // Nothing was written, so there is nothing to undo.
      return;
    }
    if (user === undefined) {
      return;
    }

    await this.#broadcastPresence(workerId, user);
    if (user.isOwner && fields.typing) {
      // The owner is at the keyboard, so the countdown to taking control from
      // them starts again.
      this.getTransferManager(workerId).resetWarning();
    }
  }

  /** Hold keystrokes from somebody who does not have control. */
  async #onQueuedInput(workerId: string, userId: string, message: Record<string, unknown>): Promise<void> {
    const store = this.getPresenceStore(workerId);
    const rawKeys = typeof message.keys === "string" ? message.keys : "";
    const display = this.getTransferManager(workerId).queueKeystroke(userId, rawKeys);
    store.update(userId, { queued_keys: display });
    const user = store.get(userId);
    if (user !== undefined) {
      // Shown to everybody, so the owner can see somebody waiting rather than
      // being surprised when control moves.
      await this.#broadcastPresence(workerId, user);
    }
  }

  /**
   * Answer a request for control.
   *
   * Three outcomes: nobody holds it, so it is granted; the holder is asking
   * again, so it is released; anybody else is asking, so nothing happens.
   * That last one is the one that matters — without it any viewer could take
   * the terminal out from under whoever is typing into it.
   */
  async #onControlRequest(workerId: string, userId: string): Promise<void> {
    const store = this.getPresenceStore(workerId);
    const owner = store.getOwner();
    if (owner === undefined) {
      store.setOwner(userId);
      // Through the transfer manager, so whatever they typed while they
      // waited travels with the handover.
      await this.#hub.broadcast(
        workerId,
        this.getTransferManager(workerId).buildTransferMessage("", userId, "handover"),
      );
    } else if (owner.userId === userId) {
      store.clearOwner();
      await this.#hub.broadcast(workerId, makeControlTransfer(userId, "", "handover"));
    }
  }

  /** Forget a session, so it does not stay in memory with its participants. */
  cleanup(workerId: string): void {
    this.#stores.delete(workerId);
    this.#managers.delete(workerId);
  }
}
