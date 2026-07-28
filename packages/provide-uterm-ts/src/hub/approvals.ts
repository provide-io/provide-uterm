//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Store for commands held pending an approve/reject decision.
 *
 * Port of the Python module `provide.uterm.server.bridge.hub.approvals` and
 * the Go package `hub`.
 *
 * The reference implementations take a lock, because a held command that is
 * approved and rejected at the same instant must be injected exactly once and
 * a check-then-set across two threads cannot promise that. Here the guarantee
 * comes from the runtime instead: {@link InMemoryApprovalStore.claim} is
 * synchronous and contains no `await`, so it runs to completion before any
 * other task observes the store. {@link InMemoryApprovalStore.cleanupExpired}
 * keeps that property deliberately — it finishes every mutation *before* it
 * awaits the first callback, so subscriber code never sees the store
 * half-updated.
 */

/** The lifecycle states an approval request moves through. */
export const APPROVAL_STATUSES = ["pending", "approved", "rejected", "timeout"] as const;

/** One of {@link APPROVAL_STATUSES}. */
export type ApprovalStatus = (typeof APPROVAL_STATUSES)[number];

/**
 * How long a request in a terminal state lingers past its expiry, in seconds.
 *
 * Resolved requests are kept for an hour so that a late reader can still learn
 * *how* a command was decided rather than finding it simply absent.
 */
export const APPROVAL_PRUNE_TTL = 3600;

/** A held command awaiting an approve/reject decision. */
export interface ApprovalRequest {
  /** Identifier the approve/reject call refers to. */
  id: string;
  /** Worker the command would be injected into. */
  workerId: string;
  /** Who submitted the command. */
  submitterId: string;
  /** The command text held for review. */
  command: string;
  /** Current lifecycle state; mutated in place by the store. */
  status: ApprovalStatus;
  /** Wall-clock seconds at submission. */
  createdAt: number;
  /** Wall-clock seconds after which the request times out. */
  expiresAt: number;
  /** Fan-out group this request belongs to, when it is part of one. */
  groupId?: string | undefined;
  /** Whether the command was submitted to a fan-out group. */
  isFanout?: boolean | undefined;
}

/** Construction options for {@link InMemoryApprovalStore}. */
export interface ApprovalStoreOptions {
  /** Wall clock in seconds. Injected so tests need not depend on real time. */
  now?: () => number;
}

/** Wall-clock seconds, matching the reference's `time.time()`. */
function wallNow(): number {
  return Date.now() / 1000;
}

/** In-memory store for approval requests, keyed by request id. */
export class InMemoryApprovalStore {
  /**
   * Notified with the id of each request that times out during cleanup.
   *
   * Subscribers (the fan-out controller, for one) use this to drop their own
   * state for the request. It runs after every mutation is complete, and is
   * awaited if it returns a promise.
   */
  onExpired: ((requestId: string) => void | Promise<void>) | undefined;

  readonly #requests = new Map<string, ApprovalRequest>();
  readonly #now: () => number;

  constructor(options: ApprovalStoreOptions = {}) {
    this.#now = options.now ?? wallNow;
  }

  /** Insert a request, replacing any existing one with the same id. */
  add(request: ApprovalRequest): void {
    this.#requests.set(request.id, request);
  }

  /** The request for `requestId`, or `undefined` when it is unknown. */
  get(requestId: string): ApprovalRequest | undefined {
    return this.#requests.get(requestId);
  }

  /**
   * Transition a pending request to `status`, ignoring it otherwise.
   *
   * Superseded by {@link claim} for handling a decision, since it cannot tell
   * the caller whether *it* was the one that resolved the request. Retained
   * because direct callers and tests still use it.
   */
  resolve(requestId: string, status: ApprovalStatus): void {
    const request = this.#requests.get(requestId);
    if (request !== undefined && request.status === "pending") {
      request.status = status;
    }
  }

  /**
   * Transition a pending request to `status`, reporting whether this call did
   * it.
   *
   * Returns `true` only for the caller that performs the transition, so a
   * command held for approval is injected exactly once even when an approve
   * and a reject arrive together. Callers must inject only on `true`.
   */
  claim(requestId: string, status: ApprovalStatus): boolean {
    const request = this.#requests.get(requestId);
    if (request === undefined || request.status !== "pending") {
      return false;
    }
    request.status = status;
    return true;
  }

  /**
   * Time out expired pending requests and prune long-dead terminal ones.
   *
   * Both comparisons are strict, so a request expiring exactly now survives
   * this pass and dies on the next one.
   */
  async cleanupExpired(): Promise<void> {
    const now = this.#now();
    const expiredIds: string[] = [];

    // Every mutation happens here, before the first callback: `onExpired` is
    // user code that may be slow, and a subscriber reading the store back
    // must not find a request it was just told about still pending.
    for (const [requestId, request] of [...this.#requests]) {
      if (request.status === "pending") {
        if (request.expiresAt < now) {
          request.status = "timeout";
          expiredIds.push(request.id);
        }
      } else if (request.expiresAt + APPROVAL_PRUNE_TTL < now) {
        this.#requests.delete(requestId);
      }
    }

    const onExpired = this.onExpired;
    if (onExpired === undefined) {
      return;
    }
    for (const requestId of expiredIds) {
      await onExpired(requestId);
    }
  }
}
