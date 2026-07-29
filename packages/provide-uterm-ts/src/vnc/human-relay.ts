//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A graphical session relayed between a browser and an upstream RFB server.
 *
 * Port of `provide.uterm.vnc.human_relay`. Upstream goes to the browser as raw
 * bytes; the browser goes upstream through {@link filterRfbClientInput}, so
 * keystrokes, pointer movement and clipboard writes are gated on a permission
 * check and a relay wired up without one forwards nothing.
 *
 * Four orderings here are load-bearing, and each is a bug if it is got wrong
 * rather than a preference.
 *
 * **The update driver waits for the client's first update request.** The
 * client's pixel format and encodings precede that request, so a driver that
 * started injecting earlier would have the server answer in its own format and
 * the client render those frames with swapped colours.
 *
 * **Every write upstream is serialised.** The driver and the filter both write
 * there, and a message split down the middle by the other is not a message.
 * The reference uses a lock across two threads; this uses a promise chain,
 * which is the same guarantee in a runtime with one.
 *
 * **Teardown stops the driver before it closes anything**, so the driver
 * cannot write into a stream being torn down.
 *
 * **A shutdown race is logged, not raised.** A closed pipe while the relay is
 * ending is the ordinary way this stops; anything else is a real fault and is
 * passed on.
 *
 * The driver exists because a client like noVNC sends one full update request
 * and then goes quiet — without it, an animating screen freezes on frame one.
 */

import { type ByteSink, type ByteSource, filterRfbClientInput, type RfbFilterOptions } from "./rfb-filter.ts";

/** How much of the upstream is moved at a time. */
export const PUMP_CHUNK = 65_536;

/** How long the driver is given to stop, in seconds. */
export const JOIN_TIMEOUT_S = 5;

/**
 * How often the driver asks upstream for an update, in seconds.
 *
 * About twenty-five a second: smooth enough for motion without flooding, and
 * an RFB server coalesces to actual damage so an idle screen stays cheap.
 */
export const DEFAULT_UPDATE_DRIVE_INTERVAL_S = 0.04;

/** How long the driver waits for the client's first request before giving up. */
export const DRIVE_HANDSHAKE_WAIT_S = 10;

/**
 * The incremental FramebufferUpdateRequest the driver injects.
 *
 * The whole surface, incremental, with width and height left at their
 * sixteen-bit maximum — the server clamps them to the real framebuffer.
 */
export const DRIVE_FBUR: Uint8Array = new Uint8Array([3, 1, 0, 0, 0, 0, 0xff, 0xff, 0xff, 0xff]);

/** Where the relay's bytes come from and go. */
export interface RelayStreams {
  /** The browser's input, filtered on its way upstream. */
  browserRead: ByteSource;
  /** The browser's screen. */
  browserWrite: ByteSink & { flush?: () => void };
  /** The upstream server's output. */
  upstreamRead: { read(size: number): Promise<Uint8Array> };
  /** The upstream server's input. */
  upstreamWrite: ByteSink & { flush?: () => void };
}

/** What a caller may say about a relay. */
export interface HumanRelayOptions extends Omit<RfbFilterOptions, "onClientReady"> {
  /** Seconds. Absent or not positive means no driver at all. */
  driveUpdateIntervalS?: number | undefined;
  /** Called once the upstream side has drained and ended. */
  onUpstreamEof?: (() => void) | undefined;
  /** Told about a shutdown race, which is not a failure. */
  onShutdownRace?: ((error: unknown) => void) | undefined;
  /** Waits, so a test can drive the clock. */
  sleep?: ((seconds: number) => Promise<void>) | undefined;
  /**
   * Moves the browser's input upstream, replacing the default.
   *
   * The default runs {@link filterRfbClientInput}, which is synchronous — it
   * reads until the source is done and cannot yield. That is right for a
   * buffered source and wrong for a live socket, where nothing else in a
   * single-threaded runtime could make progress while it ran. A caller
   * relaying a live session supplies an asynchronous equivalent here, and gets
   * the same ordering guarantees around it — provided it writes through the
   * `write` it is handed rather than touching the stream itself. That is the
   * same bargain the reference strikes by passing its lock into the filter:
   * the guarantee is only a guarantee for writes that go through it.
   */
  filterInput?:
    | ((context: { onClientReady: () => void; write: (data: Uint8Array) => Promise<void> }) => Promise<void>)
    | undefined;
}

/** Whether a pump failure is the ordinary end of a relay or a real fault. */
export function isShutdownRace(error: unknown): boolean {
  // The reference treats `OSError` and `ValueError` as races — a closed pipe,
  // and a read on a stream that has been closed under it. Classified by name
  // rather than by JavaScript's own hierarchy on purpose: a `TypeError` here
  // is a programming mistake, and swallowing it as a shutdown race would hide
  // exactly the fault worth seeing.
  const name = (error as { name?: string } | null)?.name;
  return name === "AbortError" || name === "OSError" || name === "ValueError";
}

/**
 * A queue that runs one write at a time.
 *
 * Standing in for the reference's lock: the driver and the filter both write
 * upstream, and a message interleaved with another is not a message.
 */
class WriteQueue {
  #tail: Promise<void> = Promise.resolve();

  run<T>(task: () => T | Promise<T>): Promise<T> {
    const result = this.#tail.then(task);
    // The chain continues regardless of a failure, so one bad write does not
    // wedge every write after it.
    this.#tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}

/** What a finished relay reports. */
export interface RelayResult {
  /** Whether the upstream side ended before the browser side did. */
  upstreamEnded: boolean;
  /** How many update requests the driver injected. */
  driven: number;
  /** Races that were logged rather than raised. */
  races: unknown[];
}

/**
 * Relay a graphical session until either side ends.
 *
 * @throws {Error} On a pump failure that is not a shutdown race.
 */
export async function runHumanRelay(streams: RelayStreams, options: HumanRelayOptions): Promise<RelayResult> {
  const queue = new WriteQueue();
  const races: unknown[] = [];
  const sleep =
    options.sleep ?? ((seconds: number) => new Promise<void>((resolve) => setTimeout(resolve, seconds * 1000)));

  // Assigned by the executor below, which runs before this returns.
  let resolveReady!: () => void;
  const ready = new Promise<void>((resolve) => {
    resolveReady = resolve;
  });

  let stopped = false;
  let driven = 0;
  let upstreamEnded = false;
  let pumpError: unknown;

  const pump = (async () => {
    try {
      for (;;) {
        const chunk = await streams.upstreamRead.read(PUMP_CHUNK);
        if (chunk.length === 0) {
          break;
        }
        streams.browserWrite.write(chunk);
        // Flushed after every write: a live RFB peer sends its version banner
        // and then waits, so a buffered writer would leave the browser
        // staring at nothing until the upstream closed.
        streams.browserWrite.flush?.();
      }
    } catch (error) {
      pumpError = error;
    } finally {
      upstreamEnded = true;
      // Signalled after the pump has drained: the owner tears the browser side
      // down, which is otherwise parked reading an idle browser forever once
      // the server has gone.
      try {
        options.onUpstreamEof?.();
      } catch {
        // A callback must never take the pump down with it.
      }
    }
  })();

  const driveInterval = options.driveUpdateIntervalS;
  const driving = driveInterval !== undefined && driveInterval > 0;
  const driver = driving
    ? (async () => {
        // Waits for the client's first update request, so its pixel format
        // and encodings are already upstream.
        //
        // The timeout is unobservable from outside: teardown resolves the
        // same signal, so a driver still waiting always finishes then. It is
        // kept because the reference has it and because a relay whose browser
        // side runs for hours should not hold a waiter for all of them.
        const waited = await Promise.race([ready.then(() => true), sleep(DRIVE_HANDSHAKE_WAIT_S).then(() => false)]);
        if (!waited) {
          return;
        }
        while (!stopped) {
          try {
            await queue.run(() => {
              streams.upstreamWrite.write(DRIVE_FBUR);
            });
            driven += 1;
          } catch (error) {
            // Upstream closed, or the relay is ending.
            races.push(error);
            return;
          }
          await sleep(driveInterval);
        }
      })()
    : undefined;

  try {
    const noteReady = () => resolveReady();
    if (options.filterInput !== undefined) {
      await options.filterInput({
        onClientReady: noteReady,
        write: (data) => queue.run(() => streams.upstreamWrite.write(data)),
      });
    } else {
      // Through the queue for the same reason the driver is, though nothing
      // can interleave with it as things stand: this filter is synchronous,
      // so no other write gets a turn while it runs. The queue is what makes
      // that a property of the relay rather than of the filter.
      await queue.run(() =>
        filterRfbClientInput(streams.upstreamWrite, streams.browserRead, { ...options, onClientReady: noteReady }),
      );
    }
    await queue.run(() => {
      streams.upstreamWrite.flush?.();
    });
  } finally {
    // The driver stops first, so it cannot write into a stream being torn
    // down. Setting the flag before the `await` below rather than inside it
    // is unobservable — nothing else reads it — and says the order that
    // matters rather than relying on where a block happens to end.
    stopped = true;
    resolveReady();
    // Awaited rather than abandoned. Neither can write anything more once
    // `stopped` is set, so this changes no output — what it changes is
    // whether work outlives the session that owns it.
    if (driver !== undefined) {
      await driver;
    }
    await pump;
  }

  if (pumpError !== undefined) {
    if (!isShutdownRace(pumpError)) {
      throw pumpError;
    }
    races.push(pumpError);
    options.onShutdownRace?.(pumpError);
  }
  return { upstreamEnded, driven, races };
}
