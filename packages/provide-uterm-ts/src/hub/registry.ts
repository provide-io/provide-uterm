//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * In-memory registry of attached workers, keyed by worker id.
 *
 * A deliberately thin wrapper over a `Map`: its job is to give the worker
 * table a name that the hub's other services reference, and to make every
 * read return a *snapshot*.
 *
 * That snapshot rule is the substance. Callers iterate the worker set while
 * the hub attaches and detaches workers, so handing out a live view would
 * mean iteration observing a mutation — skipped entries at best, a thrown
 * iterator at worst. `keys`, `all` and `items` therefore copy.
 *
 * The registry takes no locks. Each method is a single map operation, so
 * there is no window inside one for another caller to observe; higher-level
 * invariants that span several operations are the hub's to coordinate.
 *
 * Port of the Python module `provide.uterm.server.bridge.hub.registry` and
 * the Go package `hub`.
 */

/** Registry of worker states, keyed by worker id. */
export class WorkerRegistry<T> {
  readonly #workers = new Map<string, T>();

  /** How many workers are registered. */
  get size(): number {
    return this.#workers.size;
  }

  /** The state for `workerId`, or `undefined` when it is unknown. */
  get(workerId: string): T | undefined {
    return this.#workers.get(workerId);
  }

  /**
   * The state for `workerId`.
   *
   * @throws {Error} When the worker is unknown, for call sites where an
   *   absent worker is a bug rather than a case to handle.
   */
  require(workerId: string): T {
    const state = this.#workers.get(workerId);
    if (state === undefined) {
      throw new Error(workerId);
    }
    return state;
  }

  /** Insert or replace the state for `workerId`. */
  put(workerId: string, state: T): void {
    this.#workers.set(workerId, state);
  }

  /**
   * The existing state for `workerId`, inserting `state` if there is none.
   *
   * The existing entry wins, so an attach racing a reattach cannot discard
   * live state and hand back a fresh one.
   */
  setDefault(workerId: string, state: T): T {
    const existing = this.#workers.get(workerId);
    if (existing !== undefined) {
      return existing;
    }
    this.#workers.set(workerId, state);
    return state;
  }

  /** Remove and return the state for `workerId`, if it was present. */
  pop(workerId: string): T | undefined {
    const state = this.#workers.get(workerId);
    this.#workers.delete(workerId);
    return state;
  }

  /** Remove `workerId`, reporting whether anything was removed. */
  discard(workerId: string): boolean {
    return this.#workers.delete(workerId);
  }

  /** Whether `workerId` is registered. */
  contains(workerId: string): boolean {
    return this.#workers.has(workerId);
  }

  /** A snapshot of every registered state, in insertion order. */
  all(): T[] {
    return [...this.#workers.values()];
  }

  /** A snapshot of every registered worker id, in insertion order. */
  keys(): string[] {
    return [...this.#workers.keys()];
  }

  /** A snapshot of every (worker id, state) pair, in insertion order. */
  items(): Array<[string, T]> {
    return [...this.#workers.entries()];
  }

  /** Iterate the registered worker ids. */
  [Symbol.iterator](): Iterator<string> {
    return this.#workers.keys();
  }
}
