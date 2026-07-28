//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Fault injection, on a schedule.
 *
 * Port of the Python module `provide.uterm.transports.chaos`.
 *
 * The wrapper exists so resilience can be tested against a schedule rather
 * than against luck: which read drops, which comes back empty, and what the
 * injected error says. The jitter is drawn from this language's own generator
 * — as it already is in the Go port — so what matches across the ports is the
 * schedule, not the individual delays.
 */

import { type ConnectionTransport, TransportConnectionError } from "./base.ts";

/** How the wrapper injects faults. */
export interface ChaosOptions {
  /** Seeds the generator, so a failure can be re-run. */
  seed?: number;
  /** Inject a disconnect every N reads. Zero turns it off. */
  disconnectEveryNReceives?: number;
  /** Return empty every N reads. Zero turns it off. */
  timeoutEveryNReceives?: number;
  /** Add up to this many milliseconds of delay per read. Zero turns it off. */
  maxJitterMs?: number;
  /** Prefixes the injected error, so a log says which wrapper fired. */
  label?: string;
  /** How a delay is taken. Injected so a test need not spend real time. */
  sleep?: (seconds: number) => Promise<void>;
}

/** The label used when none is given. */
const DEFAULT_LABEL = "chaos";

/** The seed used when none is given. */
const DEFAULT_SEED = 1;

/** Wait for `seconds`. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

/**
 * A small seeded generator.
 *
 * Not cryptographic and not meant to be: it exists so the same seed replays
 * the same delays, because a failure a test cannot re-run is one nobody can
 * fix.
 */
function seededRandom(seed: number): () => number {
  let state = (seed >>> 0) + 0x6d2b79f5;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let drawn = Math.imul(state ^ (state >>> 15), 1 | state);
    drawn = (drawn + Math.imul(drawn ^ (drawn >>> 7), 61 | drawn)) ^ drawn;
    return ((drawn ^ (drawn >>> 14)) >>> 0) / 4294967296;
  };
}

/** Wraps a transport and injects faults on a schedule. */
export class ChaosTransport implements ConnectionTransport {
  readonly #inner: ConnectionTransport;
  readonly #random: () => number;
  readonly #disconnectN: number;
  readonly #timeoutN: number;
  readonly #maxJitterMs: number;
  readonly #label: string;
  readonly #sleep: (seconds: number) => Promise<void>;
  #rxCount = 0;

  constructor(inner: ConnectionTransport, options: ChaosOptions = {}) {
    this.#inner = inner;
    this.#random = seededRandom(options.seed ?? DEFAULT_SEED);
    this.#disconnectN = options.disconnectEveryNReceives ?? 0;
    this.#timeoutN = options.timeoutEveryNReceives ?? 0;
    this.#maxJitterMs = options.maxJitterMs ?? 0;
    this.#label = options.label || DEFAULT_LABEL;
    this.#sleep = options.sleep ?? realSleep;
  }

  /** Connect the inner transport. */
  async connect(host: string, port: number, options: Record<string, unknown> = {}): Promise<void> {
    await this.#inner.connect(host, port, options);
  }

  /** Disconnect the inner transport. */
  async disconnect(): Promise<void> {
    await this.#inner.disconnect();
  }

  /** Send through the inner transport. */
  async send(data: Uint8Array): Promise<void> {
    await this.#inner.send(data);
  }

  /**
   * Read, injecting whatever the schedule is due.
   *
   * The count advances before any fault is decided, so an injected fault
   * still moves the schedule along — and it is one-based, because counting
   * from zero would make every session fail on its very first read.
   *
   * @throws {TransportConnectionError} On an injected disconnect.
   */
  async receive(maxBytes: number, timeoutMs: number): Promise<Uint8Array> {
    this.#rxCount += 1;

    if (this.#maxJitterMs > 0) {
      await this.#sleep((this.#random() * this.#maxJitterMs) / 1000);
    }

    // The disconnect is checked first, so it wins when both are due: a read
    // cannot both fail and come back empty, and the harsher fault is the
    // useful one to model.
    if (this.#disconnectN > 0 && this.#rxCount % this.#disconnectN === 0) {
      try {
        await this.#inner.disconnect();
      } catch {
        // Cleanup that raised must not mask the fault the test asked for.
      }
      throw new TransportConnectionError(`${this.#label}: injected disconnect on receive #${this.#rxCount}`);
    }

    if (this.#timeoutN > 0 && this.#rxCount % this.#timeoutN === 0) {
      // Waiting out the caller's own budget, because returning empty
      // instantly would let a test pass in a way a real dead link never
      // would. Clamped, since a caller may pass a nonsense budget.
      await this.#sleep(Math.max(0, timeoutMs) / 1000);
      return new Uint8Array(0);
    }

    return await this.#inner.receive(maxBytes, timeoutMs);
  }

  /** Whether the inner transport is live — the wrapper owns no connection. */
  isConnected(): boolean {
    return this.#inner.isConnected();
  }
}
