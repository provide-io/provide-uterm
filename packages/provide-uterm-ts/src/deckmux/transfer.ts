//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Moving control between participants.
 *
 * Port of the Python module `provide.uterm.deckmux._transfer`.
 *
 * This decides who is actually typing into a live terminal, so each rule has
 * a consequence somebody watching would notice. It does not own the lease —
 * the hijack system still performs the acquire and release; this works out
 * *when* control should move and *what* the new owner is handed.
 */

import { encodeKeysDisplay, type KeystrokeQueueMode, makeControlTransfer, type TransferReason } from "./protocol.ts";

/**
 * How many keystrokes are held for somebody who cannot type yet.
 *
 * The newest are kept: showing the oldest would show them the start of what
 * they typed minutes ago instead of what they just typed.
 */
export const MAX_QUEUE_LENGTH = 256;

/** How long before the transfer a warning goes out. */
export const WARNING_LEAD_S = 10;

/** How long an owner may be idle before control moves on its own. */
export const DEFAULT_AUTO_TRANSFER_IDLE_S = 30.0;

/** What a check of the idle timer concluded. */
export interface AutoTransferDecision {
  /** Tell the owner control is about to move. */
  shouldWarn: boolean;
  /** Move it. */
  shouldTransfer: boolean;
}

/** Options for {@link TransferManager}. */
export interface TransferManagerOptions {
  /** Idle seconds before control moves. Zero or less turns it off. */
  autoTransferIdleS?: number;
  /** Whether queued keystrokes are shown or replayed. */
  keystrokeQueueMode?: KeystrokeQueueMode;
}

/** Works out when control should move, and what moves with it. */
export class TransferManager {
  readonly #autoIdleS: number;
  readonly #queueMode: KeystrokeQueueMode;
  readonly #queues = new Map<string, string>();
  #warningSent = false;

  constructor(options: TransferManagerOptions = {}) {
    this.#autoIdleS = options.autoTransferIdleS ?? DEFAULT_AUTO_TRANSFER_IDLE_S;
    this.#queueMode = options.keystrokeQueueMode ?? "display";
  }

  /** Whether control moves on its own at all. */
  get autoTransferEnabled(): boolean {
    return this.#autoIdleS > 0;
  }

  /** Whether queued keystrokes are shown or replayed. */
  get queueMode(): KeystrokeQueueMode {
    return this.#queueMode;
  }

  /**
   * Hold keystrokes from somebody who does not have control.
   *
   * @returns What the others see of them.
   */
  queueKeystroke(userId: string, rawKeys: string): string {
    let buffer = (this.#queues.get(userId) ?? "") + rawKeys;
    if (buffer.length > MAX_QUEUE_LENGTH) {
      // The tail, not the head: what they just typed is what they mean.
      buffer = buffer.slice(-MAX_QUEUE_LENGTH);
    }
    this.#queues.set(userId, buffer);
    return encodeKeysDisplay(buffer);
  }

  /** Take somebody's queue, leaving it empty. */
  flushQueue(userId: string): string {
    const buffer = this.#queues.get(userId) ?? "";
    this.#queues.delete(userId);
    return buffer;
  }

  /** Empty somebody's queue without reading it. */
  clearQueue(userId: string): void {
    this.#queues.delete(userId);
  }

  /**
   * What the others currently see of somebody's queue.
   *
   * The empty check is the reference's and is not load-bearing — rendering
   * nothing produces nothing — but it says that an absent queue and an empty
   * one are the same thing to a viewer.
   */
  getQueueDisplay(userId: string): string {
    const raw = this.#queues.get(userId) ?? "";
    return raw === "" ? "" : encodeKeysDisplay(raw);
  }

  /**
   * Decide whether to warn the owner, or take control from them.
   *
   * Nobody waiting means nothing happens: handing control to an empty queue
   * would leave the terminal orphaned. That case also re-arms the warning,
   * because the owner is no longer holding anybody up.
   */
  checkAutoTransfer(ownerIdleS: number, queuedUsers: readonly string[]): AutoTransferDecision {
    if (!this.autoTransferEnabled || queuedUsers.length === 0) {
      this.#warningSent = false;
      return { shouldWarn: false, shouldTransfer: false };
    }

    if (ownerIdleS >= this.#autoIdleS) {
      // Re-armed on the way out, so the next idle period warns again rather
      // than transferring silently.
      this.#warningSent = false;
      return { shouldWarn: false, shouldTransfer: true };
    }

    // Clamped, so a window shorter than the lead-in reads as "warn from the
    // start" rather than as a negative time. Nothing observable rests on it
    // while idle time cannot itself be negative, but a negative threshold is
    // not a thing to compare against.
    const warnThreshold = Math.max(0, this.#autoIdleS - WARNING_LEAD_S);
    if (ownerIdleS >= warnThreshold && !this.#warningSent) {
      // Once: a warning re-sent on every check is a notification storm.
      this.#warningSent = true;
      return { shouldWarn: true, shouldTransfer: false };
    }

    return { shouldWarn: false, shouldTransfer: false };
  }

  /** Arm the warning again, when the owner comes back. */
  resetWarning(): void {
    this.#warningSent = false;
  }

  /**
   * Build the message that moves control.
   *
   * The queue mode is the difference between showing what somebody typed
   * while they waited and actually typing it for them: confusing the two
   * either loses the keystrokes or runs them.
   */
  buildTransferMessage(fromUser: string, toUser: string, reason: TransferReason): Record<string, unknown> {
    if (this.#queueMode === "replay") {
      return makeControlTransfer(fromUser, toUser, reason, this.flushQueue(toUser));
    }
    const display = this.getQueueDisplay(toUser);
    this.clearQueue(toUser);
    return makeControlTransfer(fromUser, toUser, reason, display);
  }
}
