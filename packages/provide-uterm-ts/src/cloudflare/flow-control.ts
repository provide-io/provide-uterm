//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Backpressure for the Durable Object terminal relay.
 *
 * Port of the Python module
 * `provide.uterm.cloudflare.do.session_runtime.flow_control`.
 *
 * A Worker cannot see its own outbound buffer — workerd exposes no
 * `bufferedAmount` — so backpressure is driven by what the browsers say they
 * have consumed. Each reports a cumulative byte count, and what is in flight
 * is what was sent minus what was acknowledged.
 *
 * Pure logic: the session runtime wires it into the broadcast and
 * acknowledgement paths.
 */

/** Tell the producer to stop. */
export const PAUSE = "pause";

/** Tell the producer to carry on. */
export const RESUME = "resume";

/** What a check of the flow state concluded, if anything changed. */
export type FlowDecision = typeof PAUSE | typeof RESUME | undefined;

/** Options for {@link FlowController}. */
export interface FlowControllerOptions {
  /** Inflight bytes above which a browser is congested. */
  highWater: number;
  /** Inflight bytes below which it stops being. */
  lowWater: number;
  /** How long a browser may go without acknowledging before it is ignored. */
  ackGraceS: number;
}

/** Tracks what each browser has consumed and decides whether to keep sending. */
export class FlowController {
  readonly #highWater: number;
  readonly #lowWater: number;
  readonly #ackGraceS: number;
  readonly #sent = new Map<string, number>();
  readonly #acked = new Map<string, number>();
  readonly #lastAck = new Map<string, number>();
  /** Sticky per-browser congestion, set above the high mark and cleared below the low. */
  readonly #congested = new Map<string, boolean>();
  /** Browsers that just stopped being congested and need a fresh snapshot. */
  #recovered = new Set<string>();
  #paused = false;

  constructor(options: FlowControllerOptions) {
    this.#highWater = options.highWater;
    this.#lowWater = options.lowWater;
    this.#ackGraceS = options.ackGraceS;
  }

  /** Whether the producer has been told to stop. */
  get paused(): boolean {
    return this.#paused;
  }

  /** Record bytes sent to a browser. */
  onSent(wsId: string, nbytes: number): void {
    this.#sent.set(wsId, (this.#sent.get(wsId) ?? 0) + nbytes);
    this.#refreshCongestion(wsId);
  }

  /**
   * Record what a browser says it has consumed.
   *
   * Only ever forwards. These are cumulative counts, so a stale or replayed
   * one carrying a lower number must not rewind what a browser is known to
   * have consumed — that would invent congestion that is not there.
   */
  onAck(wsId: string, ackedBytes: number, now: number): void {
    this.#acked.set(wsId, Math.max(this.#acked.get(wsId) ?? 0, ackedBytes));
    this.#lastAck.set(wsId, now);
    this.#refreshCongestion(wsId);
  }

  /**
   * Drop everything known about a browser that has gone.
   *
   * Per browser, not wholesale: clearing everything would zero the accounting
   * for every tab still watching, and the producer would resume into a
   * backlog it had just been told about. The same tab id may also come back,
   * and its old sent count would look like a backlog it never received.
   *
   * Dropping the acknowledgement time alone would be enough to make the
   * browser inactive and so invisible to every decision; the rest is dropped
   * because leaving it would be leaving state for something that has gone.
   */
  forget(wsId: string): void {
    this.#sent.delete(wsId);
    this.#acked.delete(wsId);
    this.#lastAck.delete(wsId);
    this.#congested.delete(wsId);
    this.#recovered.delete(wsId);
  }

  /** What one browser has outstanding. */
  #inflight(wsId: string): number {
    return (this.#sent.get(wsId) ?? 0) - (this.#acked.get(wsId) ?? 0);
  }

  /**
   * Whether a browser has acknowledged recently enough to be listened to.
   *
   * The existence check cannot fail for the callers here — both walk the
   * acknowledgement times — but it is what makes "never acknowledged" mean
   * inactive rather than "acknowledged at the epoch".
   */
  #isActive(wsId: string, now: number): boolean {
    const last = this.#lastAck.get(wsId);
    return last !== undefined && now - last <= this.#ackGraceS;
  }

  /**
   * The largest amount outstanding among browsers still acknowledging.
   *
   * A silent one is left out: it looks maximally congested for ever, and
   * counting it would report a backlog that nobody is actually waiting on.
   */
  maxInflight(now: number): number {
    let best = 0;
    for (const wsId of this.#lastAck.keys()) {
      if (!this.#isActive(wsId, now)) {
        continue;
      }
      best = Math.max(best, this.#inflight(wsId));
    }
    return best;
  }

  /**
   * Update one browser's sticky congestion.
   *
   * It becomes congested above the high mark and stays so until it drains
   * below the low one. Without that gap it would flap either side of a single
   * threshold, pausing and resuming on every frame.
   */
  #refreshCongestion(wsId: string): void {
    const inflight = this.#inflight(wsId);
    if (this.#congested.get(wsId) !== true) {
      if (inflight > this.#highWater) {
        this.#congested.set(wsId, true);
      }
    } else if (inflight < this.#lowWater) {
      this.#congested.set(wsId, false);
      // It missed frames while congested, so it needs a fresh snapshot.
      this.#recovered.add(wsId);
    }
  }

  /** Whether droppable frames to this browser should be skipped. */
  isCongested(wsId: string): boolean {
    return this.#congested.get(wsId) === true;
  }

  /**
   * Whether every browser still acknowledging is congested.
   *
   * If even the fastest consumer can keep up there is something worth
   * producing — pausing on the slowest would let one browser throttle
   * everybody else's session. A session with nobody acknowledging returns
   * false, so a stuck client can never stall the producer for good.
   */
  allActiveCongested(now: number): boolean {
    // Walked over the acknowledgement times rather than the sent counts: a
    // browser that has never acknowledged is not active either way, and this
    // says which question is being asked.
    const active = [...this.#lastAck.keys()].filter((wsId) => this.#isActive(wsId, now));
    return active.length > 0 && active.every((wsId) => this.isCongested(wsId));
  }

  /**
   * Whether to tell the producer anything.
   *
   * Reports a change once rather than on every check, so the producer is not
   * told to pause repeatedly while it already is.
   */
  decide(now: number): FlowDecision {
    const congested = this.allActiveCongested(now);
    if (!this.#paused && congested) {
      this.#paused = true;
      return PAUSE;
    }
    if (this.#paused && !congested) {
      this.#paused = false;
      return RESUME;
    }
    return undefined;
  }

  /**
   * The browsers that have just stopped being congested, taken and cleared.
   *
   * Each needs a snapshot to replace what it missed; reporting one twice
   * would send two.
   */
  takeRecovered(): Set<string> {
    const out = this.#recovered;
    this.#recovered = new Set();
    return out;
  }
}
