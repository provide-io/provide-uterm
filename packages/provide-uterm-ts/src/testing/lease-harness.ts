//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Test harness for {@link HijackLeaseManager}.
 *
 * The manager reaches back into the hub for every cross-cutting effect —
 * broadcast, event append, worker send, prune, metrics. This supplies a
 * recording stand-in for that surface plus a worker socket that can be made
 * to fail, which is what the two lease test files drive.
 *
 * It lives in `testing/` rather than in a test file because both
 * `lease.test.ts` and `lease-lifecycle.test.ts` need it, and because the
 * recorded call *order* is a property the tests assert on.
 */

import type { LeaseHubCallbacks } from "../hub/lease.ts";
import type { Connection, HijackSession, WorkerTermState } from "../hub/models.ts";

/** A worker socket that records what it was sent, and can be made to fail. */
export class FakeWorkerSocket {
  /** Frames written to this socket, in order. */
  readonly sent: string[] = [];

  readonly #fail: boolean;

  constructor(fail = false) {
    this.#fail = fail;
  }

  /** Write a frame, or reject when this socket was built to fail. */
  async sendText(payload: string): Promise<void> {
    if (this.#fail) {
      throw new Error("socket closed");
    }
    this.sent.push(payload);
  }
}

/** One recorded call into the hub surface. */
export interface RecordedCall {
  call: string;
  workerId?: string;
  name?: string;
  eventType?: string;
  action?: string;
}

/** A recording stand-in for the hub surface the lease manager calls back into. */
export class FakeLeaseHub implements LeaseHubCallbacks {
  /** Every call made, in order — the order is asserted on. */
  readonly calls: RecordedCall[] = [];
  /** Messages handed to {@link sendWorker}. */
  readonly sent: Array<Record<string, unknown>> = [];
  /** Forces {@link isHijacked}, for the concurrent-acquire recheck. */
  hijackedOverride: boolean | undefined;

  isDashboardHijackActive(state: WorkerTermState): boolean {
    if (state.hijackOwner === undefined) {
      return false;
    }
    if (state.hijackOwnerExpiresAt === undefined) {
      return true;
    }
    return state.hijackOwnerExpiresAt > this.now();
  }

  hasValidRestLease(state: WorkerTermState): boolean {
    const session = state.hijackSession;
    return session !== undefined && session.leaseExpiresAt > this.now();
  }

  isHijacked(state: WorkerTermState): boolean {
    return this.hijackedOverride ?? (this.isDashboardHijackActive(state) || this.hasValidRestLease(state));
  }

  canSendInput(state: WorkerTermState, ws: Connection): boolean {
    if (state.inputMode === "open") {
      const role = state.browsers.get(ws) ?? "viewer";
      return role === "operator" || role === "admin";
    }
    return this.isDashboardHijackActive(state) && state.hijackOwner === ws;
  }

  metric(name: string): void {
    this.calls.push({ call: "metric", name });
  }

  notifyHijackChanged(workerId: string): void {
    this.calls.push({ call: "notify_hijack_changed", workerId });
  }

  async sendWorker(workerId: string, message: Record<string, unknown>): Promise<boolean> {
    this.sent.push(message);
    this.calls.push({ call: "send_worker", workerId, action: String(message["action"] ?? "") });
    return true;
  }

  async broadcastHijackState(workerId: string): Promise<void> {
    this.calls.push({ call: "broadcast_hijack_state", workerId });
  }

  async appendEvent(workerId: string, eventType: string): Promise<Record<string, unknown>> {
    this.calls.push({ call: "append_event", workerId, eventType });
    return {};
  }

  async pruneIfIdle(workerId: string): Promise<void> {
    this.calls.push({ call: "prune_if_idle", workerId });
  }

  async recheckAndResume(workerId: string): Promise<void> {
    this.calls.push({ call: "recheck_and_resume", workerId });
  }

  /** The frozen clock the predicates read; overwritten by the test setup. */
  now: () => number = () => 0;

  /** The recorded call names, with append_event tagged by its event type. */
  callNames(): string[] {
    return this.calls.map((call) => (call.call === "append_event" ? `append_event:${call.eventType}` : call.call));
  }
}

/** A REST lease expiring at `expiresAt`. */
export function session(expiresAt: number, owner = "operator", hijackId = "h1"): HijackSession {
  return { hijackId, owner, acquiredAt: 0, leaseExpiresAt: expiresAt, lastHeartbeat: 0 };
}
