//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Per-worker state the hub holds, and the lease view over its hijack fields.
 *
 * Port of the Python module `provide.uterm.server.bridge.models` and the Go
 * package `hub` (`models.go`).
 *
 * Connections are compared by identity, matching the reference's `is` checks
 * and its use of the WebSocket object as a dict key. Nothing here takes a
 * lock: the composing hub serialises access, exactly as the reference
 * documents.
 */

/** Roles a browser may hold on a worker session. */
export type BrowserRole = "viewer" | "operator" | "admin";

/** The roles the hub accepts on a browser connection. */
export const VALID_ROLES: ReadonlySet<BrowserRole> = new Set<BrowserRole>(["viewer", "operator", "admin"]);

/**
 * Whether input is gated behind a hijack lease or open to operators.
 *
 * In `open` mode viewers still cannot send; the gate moves from the lease to
 * the role.
 */
export type InputMode = "hijack" | "open";

/** Default bound on a worker's retained event log. */
export const EVENT_DEQUE_MAXLEN = 2000;

/**
 * A browser or worker connection.
 *
 * Only its identity matters here — the hub keys maps on it and compares it
 * with `===`, mirroring the reference's `is` comparisons against the FastAPI
 * WebSocket object.
 */
export type Connection = object;

/**
 * The worker side of a session.
 *
 * Only the write half is modelled: it is all the hub's lease and routing
 * paths use, and narrowing it here keeps them free of a transport type.
 */
export interface WorkerSocket extends Connection {
  /** Write an already-encoded frame to the worker. */
  sendText(payload: string): Promise<void>;
}

/** A live REST hijack lease. */
export interface HijackSession {
  /** Identifier the heartbeat and release calls refer to. */
  hijackId: string;
  /** Self-declared display label for whoever holds the lease. */
  owner: string;
  /** Monotonic seconds after which the lease is no longer active. */
  leaseExpiresAt: number;
  /** When the lease was first taken. */
  acquiredAt?: number;
  /** When the lease was last refreshed. */
  lastHeartbeat?: number;
  /**
   * Authenticated subject id of the acquiring principal.
   *
   * Distinct from {@link owner}, which is self-declared: this is what release
   * checks ownership against. Absent for unauthenticated or legacy leases.
   */
  acquiredBy?: string | undefined;
}

/** Construction options for {@link WorkerTermState}. */
export interface WorkerTermStateOptions {
  /** Monotonic clock in seconds, seeding {@link WorkerTermState.lastActivityAt}. */
  now?: () => number;
}

/** Monotonic seconds, from a clock that cannot jump backwards. */
function monotonicNow(): number {
  return performance.now() / 1000;
}

/** Construction options for {@link HijackLease}. */
export interface HijackLeaseOptions {
  /** Dashboard WebSocket holding the lease. */
  ws?: Connection | undefined;
  /** Monotonic expiry of the dashboard lease. */
  wsExpiresAt?: number | undefined;
  /** REST lease, when one is held. */
  session?: HijackSession | undefined;
}

/**
 * A fixed-capacity log that drops its oldest entry when it overflows.
 *
 * Reproduces `collections.deque(maxlen=...)` for the one use the hub makes of
 * it: append to the back, drop from the front, and rebuild with a different
 * bound. Backed by a ring buffer so a push at capacity stays O(1) — this runs
 * once per worker event.
 *
 * {@link at} follows the JavaScript convention rather than the Python one: an
 * out-of-range index yields `undefined` where a `deque` would raise.
 */
export class BoundedDeque<T> {
  readonly #maxlen: number;
  readonly #slots: Array<T | undefined>;
  /** Index of the oldest entry. */
  #head = 0;
  #length = 0;

  constructor(maxlen: number) {
    if (maxlen < 1) {
      // A zero-length log would silently discard every event handed to it,
      // which is a misconfiguration rather than a policy.
      throw new RangeError(`maxlen must be at least 1, got ${maxlen}`);
    }
    this.#maxlen = maxlen;
    this.#slots = new Array<T | undefined>(maxlen);
  }

  /** The capacity this log was built with. */
  get maxlen(): number {
    return this.#maxlen;
  }

  /** How many entries it currently holds. */
  get length(): number {
    return this.#length;
  }

  /** Append to the back, dropping the front when already at capacity. */
  push(item: T): void {
    const slot = (this.#head + this.#length) % this.#maxlen;
    this.#slots[slot] = item;
    if (this.#length === this.#maxlen) {
      this.#head = (this.#head + 1) % this.#maxlen;
    } else {
      this.#length += 1;
    }
  }

  /** The entry at `index`, counting from the back when negative. */
  at(index: number): T | undefined {
    const offset = index < 0 ? this.#length + index : index;
    if (offset < 0 || offset >= this.#length) {
      return undefined;
    }
    return this.#slots[(this.#head + offset) % this.#maxlen];
  }

  /** A snapshot of every entry, oldest first. */
  toArray(): T[] {
    const items: T[] = [];
    for (let offset = 0; offset < this.#length; offset += 1) {
      items.push(this.#slots[(this.#head + offset) % this.#maxlen] as T);
    }
    return items;
  }

  /**
   * A copy bounded by `maxlen`, keeping the newest entries.
   *
   * The hub rebuilds a worker's event log this way on connect, with its own
   * configured bound. Truncation must take from the front: the newest events
   * are the ones a resuming browser still needs.
   */
  withMaxlen(maxlen: number): BoundedDeque<T> {
    const rebuilt = new BoundedDeque<T>(maxlen);
    for (const item of this.toArray()) {
      rebuilt.push(item);
    }
    return rebuilt;
  }

  /** Iterate the entries, oldest first. */
  *[Symbol.iterator](): Generator<T> {
    yield* this.toArray();
  }
}

/**
 * A view over the three hijack fields of a {@link WorkerTermState}.
 *
 * Ownership reaches the same slot by two independent paths — a dashboard
 * WebSocket lease and a REST session lease — and only one is active at a
 * time. This groups them so the state-machine questions ("is anyone holding
 * this? has it lapsed?") have somewhere to live.
 *
 * It *borrows* the slots rather than owning them: {@link WorkerTermState.lease}
 * builds a fresh view on every read and mutations do not propagate back.
 * {@link WorkerTermState.applyLease} is the way back. That asymmetry is
 * deliberate — a read-only predicate caller must not be able to release
 * someone else's hijack by accident.
 *
 * Every method takes `now` explicitly so none of them read a clock.
 */
export class HijackLease {
  /** Dashboard WebSocket holding the lease. */
  ws: Connection | undefined;
  /** Monotonic expiry of the dashboard lease. */
  wsExpiresAt: number | undefined;
  /** REST lease, when one is held. */
  session: HijackSession | undefined;

  constructor(options: HijackLeaseOptions = {}) {
    this.ws = options.ws;
    this.wsExpiresAt = options.wsExpiresAt;
    this.session = options.session;
  }

  /** Whether neither slot is occupied. */
  get isIdle(): boolean {
    return this.ws === undefined && this.session === undefined;
  }

  /** Whether the dashboard slot is occupied and unexpired at `now`. */
  isDashboardActive(now: number): boolean {
    if (this.ws === undefined || this.wsExpiresAt === undefined) {
      return false;
    }
    return this.wsExpiresAt > now;
  }

  /** Whether the REST slot is occupied and unexpired at `now`. */
  isRestActive(now: number): boolean {
    if (this.session === undefined) {
      return false;
    }
    return this.session.leaseExpiresAt > now;
  }

  /** Whether either sub-lease is active at `now`. */
  isActive(now: number): boolean {
    return this.isDashboardActive(now) || this.isRestActive(now);
  }

  /**
   * Clear whichever sub-leases have lapsed, reporting which ones did.
   *
   * The comparison is `<=` where the active predicates use `>`, so a lease
   * expiring at exactly `now` is both inactive and expired. An unoccupied
   * slot reports `false`: clearing nothing is not an expiry event, and the
   * callers use these flags for telemetry.
   *
   * An occupied dashboard slot carrying no expiry is left alone — it is
   * neither active nor expirable, and releasing it belongs to the connection
   * lifecycle.
   */
  expire(now: number): { restExpired: boolean; dashExpired: boolean } {
    const restExpired = this.session !== undefined && this.session.leaseExpiresAt <= now;
    const dashExpired = this.ws !== undefined && this.wsExpiresAt !== undefined && this.wsExpiresAt <= now;
    if (restExpired) {
      this.session = undefined;
    }
    if (dashExpired) {
      this.ws = undefined;
      this.wsExpiresAt = undefined;
    }
    return { restExpired, dashExpired };
  }
}

/**
 * Per-worker connection state held by the hub's registry.
 *
 * The hijack fields are kept flat rather than behind {@link lease} because
 * that is how the reference stores them and how its call sites read them;
 * new code should prefer the lease view.
 */
export class WorkerTermState {
  /** The worker's own socket, once it has connected. */
  workerWs: WorkerSocket | undefined;
  /** Attached browsers and the role each holds. */
  browsers = new Map<Connection, BrowserRole>();
  /** Dashboard WebSocket holding the hijack lease. */
  hijackOwner: Connection | undefined;
  /** Monotonic expiry of the dashboard hijack lease. */
  hijackOwnerExpiresAt: number | undefined;
  /** REST hijack lease, when one is held. */
  hijackSession: HijackSession | undefined;
  /**
   * Transient REST-acquire reservation.
   *
   * Set while an acquire pauses the worker outside the hub lock, then cleared
   * when the lease is finalised or rolled back. It makes the acquire mutually
   * exclusive without holding the lock across the pause.
   */
  hijackPending: string | undefined;
  /** Whether input is gated behind the lease or open to operators. */
  inputMode: InputMode = "hijack";
  /**
   * Whether an authenticated caller has explicitly decided this session's input
   * mode, as opposed to it merely holding the `"hijack"` default above.
   *
   * This tells two claims apart. A `worker_hello` announces what the worker
   * process booted with; `setInputMode` is a decision made through an
   * authenticated route by somebody holding `session.control.mode`. Without the
   * distinction the hub cannot refuse a hello that lowers `hijack` to `open`,
   * because `inputMode` defaults to `hijack` and refusing every lowering would
   * refuse every worker that legitimately announces `open`.
   *
   * Held on the worker state rather than the connection deliberately: registry
   * state outlives a worker socket, so a decision survives a reconnect.
   * Internal only — nothing serialises it onto the wire.
   */
  inputModeSetByOperator = false;
  /** Most recent screen snapshot received from the worker. */
  lastSnapshot: Record<string, unknown> | undefined;
  /** Retained event log, bounded so a long-lived worker cannot grow forever. */
  events = new BoundedDeque<Record<string, unknown>>(EVENT_DEQUE_MAXLEN);
  /** Sequence number of the most recent event. */
  eventSeq = 0;
  /** Sequence number of the oldest event still retained. */
  minEventSeq = 0;
  /**
   * Monotonic seconds of the last activity, for idle pruning.
   *
   * Seeded from the clock rather than from zero: a worker that has connected
   * but not yet spoken must not read as infinitely idle to the pruner.
   */
  lastActivityAt: number;
  /** Protocol version agreed at the worker handshake. */
  protocolVersion: number | undefined;
  /**
   * Whether the worker arrived over the binary-framed tunnel.
   *
   * Tunnel workers take raw PTY bytes rather than a framed JSON envelope, so
   * the send path branches on this.
   */
  isTunnelWorker = false;
  /** Attached graphical console session, once a target is attached. */
  graphicalSession: unknown;

  constructor(options: WorkerTermStateOptions = {}) {
    this.lastActivityAt = (options.now ?? monotonicNow)();
  }

  /** A fresh {@link HijackLease} view over this state's hijack fields. */
  get lease(): HijackLease {
    return new HijackLease({
      ws: this.hijackOwner,
      wsExpiresAt: this.hijackOwnerExpiresAt,
      session: this.hijackSession,
    });
  }

  /** Write a view's slots back onto this state's hijack fields. */
  applyLease(lease: HijackLease): void {
    this.hijackOwner = lease.ws;
    this.hijackOwnerExpiresAt = lease.wsExpiresAt;
    this.hijackSession = lease.session;
  }
}
