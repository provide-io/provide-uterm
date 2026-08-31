//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Accumulating one session's output after a fan-out send.
 *
 * Port of the Python module `provide.uterm.server.bridge.fanout._collector`.
 *
 * The wait is adaptive rather than fixed: a session that has finished talking
 * should not cost the caller the full hard cap, and one that never stops must
 * not hold the whole fan-out open.
 */

/** A live subscription to one worker's term and snapshot events. */
export interface EventSubscription {
  /**
   * The next event, `undefined` if none arrived within `timeoutMs`, or `null`
   * when the worker has disconnected.
   */
  next(timeoutMs: number): Promise<Record<string, unknown> | undefined | null>;
  /**
   * Events already buffered and not yet handed back by {@link next}.
   *
   * Required rather than optional: it is the only signal that separates a
   * member still talking from one that finished, and a subscription allowed
   * to stay silent about it would report every truncated response as
   * complete. Every bus this models has it -- a queue length in Go, `qsize`
   * in Python.
   */
  pending(): number;
  /** Release the subscription's queue on the bus. */
  close(): Promise<void>;
}

/** Options for {@link collectOutput}. */
export interface CollectOutputOptions {
  /** Opens the subscription. Absent when the hub has no event bus. */
  subscribe?: () => Promise<EventSubscription>;
  /** Monotonic clock in seconds. */
  now?: () => number;
  /** How long the stream must be silent before collection ends. */
  quiesceMs: number;
  /** Hard cap on total collection time. */
  maxMs: number;
}

/** What a session produced, and how long it took. */
export interface CollectedOutput {
  /** Everything the session printed. */
  output: string;
  /** Wall-clock time spent collecting. */
  elapsedMs: number;
}

/** An output subscription opened before worker dispatch. */
export interface OutputCapture {
  /**
   * Whether the last {@link collect} stopped with output still queued.
   *
   * The controller reports such a member as not ok: a response cut off at the
   * budget is not a complete one, and returning it as ok makes truncation
   * indistinguishable from a member that simply finished quickly.
   */
  deadlineExceeded: boolean;
  /** Collect from the already-open subscription. */
  collect(options: Omit<CollectOutputOptions, "subscribe"> & { startedAt?: number }): Promise<CollectedOutput>;
  /** Close the subscription exactly once. */
  close(): Promise<void>;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** The string at `key` in an event payload, or empty when there is none. */
function textField(event: Record<string, unknown>, key: string): string {
  const data = event.data;
  // Events come off a bus shared with other producers; a malformed payload
  // must not throw in the middle of a fan-out.
  if (typeof data !== "object" || data === null) {
    return "";
  }
  const value = (data as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

/**
 * Accumulate a session's output until it quiesces or the cap is reached.
 *
 * Ends on whichever comes first: silence for `quiesceMs`, a total of `maxMs`,
 * or the worker disconnecting. Waiting out the quiesce window for a session
 * that has already gone is pure latency, so the disconnect sentinel stops it
 * immediately.
 *
 * `term` deltas are the primary source. When only snapshots arrive — which is
 * what the shell and SSH control connectors produce, since they never emit
 * term events — the last screen is returned instead, so a fan-out over those
 * sessions is not silently empty.
 *
 * With no subscription there is no bus, which is a valid configuration rather
 * than an error.
 */
export async function openOutputCapture(
  options: Pick<CollectOutputOptions, "subscribe" | "now">,
): Promise<OutputCapture | undefined> {
  if (options.subscribe === undefined) {
    return undefined;
  }
  const now = options.now ?? monotonicNow;
  const subscription = await options.subscribe();
  let closed = false;
  const capture: OutputCapture = {
    deadlineExceeded: false,
    async collect(collectOptions): Promise<CollectedOutput> {
      capture.deadlineExceeded = false;
      const start = collectOptions.startedAt ?? now();
      const termChunks: string[] = [];
      let lastSnapshotScreen = "";
      for (;;) {
        const remainingMs = collectOptions.maxMs - (now() - start) * 1000;
        if (remainingMs <= 0) {
          break;
        }
        const event = await subscription.next(Math.min(remainingMs, collectOptions.quiesceMs));
        if (event === undefined || event === null) {
          break;
        }
        if (event.type === "term") {
          const text = textField(event, "data");
          if (text !== "") {
            termChunks.push(text);
          }
          continue;
        }
        const screen = textField(event, "screen");
        if (screen !== "") {
          lastSnapshotScreen = screen;
        }
      }
      // Truncated means we stopped with more still queued -- the member had
      // not finished talking. Deriving it from what is left, rather than from
      // WHICH exit fired, is what makes it reliable: the loop only reaches the
      // exhausted-budget exit if an event happens to land in the final
      // microseconds, so keying on that loses the truncation whenever the
      // producer stalls near the end. It also keeps a group whose quiesce is
      // longer than its cap correct, since a member that answered and went
      // quiet leaves nothing pending. Go's collector pins exactly this case.
      capture.deadlineExceeded = subscription.pending() > 0;
      return {
        output: termChunks.length > 0 ? termChunks.join("") : lastSnapshotScreen,
        elapsedMs: Math.trunc((now() - start) * 1000),
      };
    },
    async close(): Promise<void> {
      if (closed) {
        return;
      }
      closed = true;
      await subscription.close();
    },
  };
  return capture;
}

export async function collectOutput(options: CollectOutputOptions): Promise<CollectedOutput> {
  const capture = await openOutputCapture(options);
  if (capture === undefined) {
    return { output: "", elapsedMs: 0 };
  }
  try {
    return await capture.collect(options);
  } finally {
    await capture.close();
  }
}
