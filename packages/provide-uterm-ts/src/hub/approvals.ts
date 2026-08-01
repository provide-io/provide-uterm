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
export const APPROVAL_STATUSES = ["pending", "resolving", "approved", "rejected", "refused", "timeout"] as const;

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
  /** Current lifecycle state; the store mutates only its internal copy. */
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

/** A stored request carrying the opaque generation that fences stale work. */
export interface StoredApprovalRequest extends ApprovalRequest {
  /** Store-assigned generation used to fence stale decisions and callbacks. */
  readonly revision: number;
}

/** Explicit name for the public construction shape. */
export type ApprovalRequestInput = ApprovalRequest;

/** Construction options for {@link InMemoryApprovalStore}. */
export interface ApprovalStoreOptions {
  /** Wall clock in seconds. Injected so tests need not depend on real time. */
  now?: () => number;
  /** Last issued revision when restoring a store's monotonic sequence. */
  initialRevision?: number;
}

/** Wall-clock seconds, matching the reference's `time.time()`. */
function wallNow(): number {
  return Date.now() / 1000;
}

/** In-memory store for approval requests, keyed by request id. */
export class InMemoryApprovalStore {
  /**
   * Notified with an exact-revision snapshot of each request that times out.
   *
   * Subscribers (the fan-out controller, for one) use this to drop their own
   * state for the request. It runs after every mutation is complete, and is
   * awaited if it returns a promise.
   */
  onExpired: ((request: StoredApprovalRequest) => void | Promise<void>) | undefined;

  readonly #requests = new Map<string, StoredApprovalRequest>();
  /**
   * Exact-revision snapshots of requests that timed out, awaiting delivery.
   *
   * A claim or resolve that finds its request already expired times it out on
   * the spot, but the notification is queued rather than delivered — the
   * reference never runs listener code inside a decision call. A decision
   * route drains it with {@link notifyExpired} the moment its claim fails;
   * {@link cleanupExpired} drains whatever nobody claimed. Keyed by id *and*
   * revision, so however many drains follow, each expiry goes out once.
   */
  readonly #expiredNotifications = new Map<string, StoredApprovalRequest>();
  readonly #now: () => number;
  #nextRevision: number;

  constructor(options: ApprovalStoreOptions = {}) {
    this.#now = options.now ?? wallNow;
    const initialRevision = options.initialRevision ?? 0;
    if (!Number.isSafeInteger(initialRevision) || initialRevision < 0) {
      throw new RangeError("initial approval revision must be a non-negative safe integer");
    }
    this.#nextRevision = initialRevision;
  }

  /**
   * Insert a request and return its store-assigned revision.
   *
   * A live id collision is rejected instead of replacing the current
   * request: replacement would let a delayed decision for the old request
   * act on the new one. Once a terminal request is pruned, the id may be
   * reused, but it receives a fresh revision.
   */
  add(request: ApprovalRequestInput): StoredApprovalRequest | undefined {
    if (this.#requests.has(request.id)) {
      return undefined;
    }
    if (this.#nextRevision === Number.MAX_SAFE_INTEGER) {
      throw new RangeError("approval revision space exhausted");
    }
    this.#nextRevision += 1;
    const stored = { ...request, revision: this.#nextRevision };
    this.#requests.set(stored.id, stored);
    return { ...stored };
  }

  /** Time a request out and queue its exact-revision snapshot for delivery. */
  #expire(request: StoredApprovalRequest): void {
    request.status = "timeout";
    this.#expiredNotifications.set(`${request.id}#${request.revision}`, { ...request });
  }

  /** A snapshot of `requestId`, or `undefined` when it is unknown. */
  get(requestId: string): StoredApprovalRequest | undefined {
    const request = this.#requests.get(requestId);
    return request === undefined ? undefined : { ...request };
  }

  /**
   * Transition the exact pending revision to `status`, ignoring it otherwise.
   *
   * Superseded by {@link claim} for handling a decision, since it cannot tell
   * the caller whether *it* was the one that resolved the request. Retained
   * because direct callers and tests still use it.
   */
  resolve(requestId: string, status: ApprovalStatus, expectedRevision: number): void {
    const request = this.#requests.get(requestId);
    if (request === undefined || request.status !== "pending" || request.revision !== expectedRevision) {
      return;
    }
    if (request.expiresAt <= this.#now()) {
      this.#expire(request);
      return;
    }
    request.status = status;
  }

  /**
   * Transition the exact pending revision to `status`, reporting whether this
   * call did it.
   *
   * Returns `true` only for the caller that performs the transition, so a
   * command held for approval is injected exactly once even when an approve
   * and a reject arrive together. Callers must inject only on `true`.
   */
  claim(requestId: string, status: ApprovalStatus, expectedRevision: number): boolean {
    const request = this.#requests.get(requestId);
    if (request === undefined || request.status !== "pending" || request.revision !== expectedRevision) {
      return false;
    }
    // An expired request cannot be decided any more — it is timed out on the
    // spot, so a late approve arriving after the window closes injects
    // nothing.
    if (request.expiresAt <= this.#now()) {
      this.#expire(request);
      return false;
    }
    request.status = status;
    return true;
  }

  /**
   * Reserve the exact pending revision for resolution, returning its snapshot.
   *
   * The claiming half of the two-phase decision: `pending` becomes
   * `resolving`, and the caller is handed the request it now owns so it can
   * act on the command before {@link finalize} writes the outcome. Anyone
   * else claiming that revision meanwhile gets `undefined`, so the command
   * behind it is acted on exactly once. The snapshot is a copy — the store
   * keeps its own.
   */
  claimRequest(requestId: string, status: ApprovalStatus, expectedRevision: number): StoredApprovalRequest | undefined {
    const request = this.#requests.get(requestId);
    if (request === undefined || request.status !== "pending" || request.revision !== expectedRevision) {
      return undefined;
    }
    if (request.expiresAt <= this.#now()) {
      this.#expire(request);
      return undefined;
    }
    request.status = status;
    return { ...request };
  }

  /**
   * Write the outcome of a request that {@link claimRequest} reserved.
   *
   * Only `approved` and `refused` are outcomes; anything else is a caller
   * mistake rather than a decision, and is refused loudly rather than
   * recorded. Only the exact revision left in `resolving` moves, so a
   * finalize arriving twice — or for a request nobody claimed — writes
   * nothing.
   */
  finalize(requestId: string, status: ApprovalStatus, expectedRevision: number): boolean {
    if (status !== "approved" && status !== "refused") {
      throw new RangeError("approval resolution must finalize as approved or refused");
    }
    const request = this.#requests.get(requestId);
    if (request === undefined || request.status !== "resolving" || request.revision !== expectedRevision) {
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

    // Every mutation happens here, before the first callback: `onExpired` is
    // user code that may be slow, and a subscriber reading the store back
    // must not find a request it was just told about still pending.
    for (const [requestId, request] of [...this.#requests]) {
      if (request.status === "pending") {
        if (request.expiresAt < now) {
          this.#expire(request);
        }
      } else if (request.expiresAt + APPROVAL_PRUNE_TTL < now) {
        this.#requests.delete(requestId);
      }
    }

    // This pass also delivers the timeouts a claim or resolve queued earlier.
    await this.notifyExpired();
  }

  /**
   * Deliver every queued expiry snapshot, exactly once.
   *
   * A decision route calls this straight after a claim that failed, so the
   * browser that was told "too late" learns the request timed out then rather
   * than whenever the next cleanup sweep happens to run. The queue is emptied
   * before the first callback, so a second call — or the cleanup sweep — has
   * nothing left to deliver and cannot notify the same expiry twice.
   */
  async notifyExpired(): Promise<void> {
    const expired = [...this.#expiredNotifications.values()];
    this.#expiredNotifications.clear();
    const onExpired = this.onExpired;
    if (onExpired === undefined) {
      return;
    }
    for (const request of expired) {
      await onExpired({ ...request });
    }
  }
}
