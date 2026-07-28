//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Per-session presence.
 *
 * Port of the Python module `provide.uterm.deckmux._presence`.
 *
 * This is the one place a *browser* writes into memory the server then hands
 * to everybody else, so the untrusted values are bounded and a rejected update
 * changes nothing at all.
 */

import { pyJsonDumps } from "../pycompat/index.ts";
import { makePresenceSync } from "./protocol.ts";

/**
 * Largest a `selection` or `pin` may encode to.
 *
 * A legitimate selection is a handful of small integers. The value is stored
 * and re-broadcast verbatim to every joiner, so an unbounded one is memory
 * amplification with an audience.
 */
export const MAX_PRESENCE_DICT_BYTES = 2048;

/** Most top-level keys such a value may carry. */
export const MAX_PRESENCE_DICT_KEYS = 16;

/** The fields whose values come straight from a browser. */
export const VALIDATED_PRESENCE_FIELDS: readonly string[] = ["pin", "selection"];

const VALIDATED = new Set(VALIDATED_PRESENCE_FIELDS);

/** One person in a session. */
export interface UserPresence {
  userId: string;
  name: string;
  color: string;
  role: string;
  initials: string;
  scrollLine: number;
  scrollRange: [number, number];
  totalLines: number;
  selection?: Record<string, unknown> | undefined;
  pin?: Record<string, unknown> | undefined;
  typing: boolean;
  queuedKeys: string;
  cols: number;
  rows: number;
  lastActivityAt: number;
  isOwner: boolean;
}

/** The wire names, in the order the reference emits them. */
const WIRE_FIELDS: ReadonlyArray<[keyof UserPresence, string]> = [
  ["userId", "user_id"],
  ["name", "name"],
  ["color", "color"],
  ["role", "role"],
  ["initials", "initials"],
  ["scrollLine", "scroll_line"],
  ["scrollRange", "scroll_range"],
  ["totalLines", "total_lines"],
  ["selection", "selection"],
  ["pin", "pin"],
  ["typing", "typing"],
  ["queuedKeys", "queued_keys"],
  ["cols", "cols"],
  ["rows", "rows"],
  ["isOwner", "is_owner"],
];

/** Which stored field a caller's name refers to. */
const FIELD_NAMES: Readonly<Record<string, keyof UserPresence>> = {
  user_id: "userId",
  name: "name",
  color: "color",
  role: "role",
  initials: "initials",
  scroll_line: "scrollLine",
  scroll_range: "scrollRange",
  total_lines: "totalLines",
  selection: "selection",
  pin: "pin",
  typing: "typing",
  queued_keys: "queuedKeys",
  cols: "cols",
  rows: "rows",
  last_activity_at: "lastActivityAt",
  is_owner: "isOwner",
};

/** A presence record as it goes over the wire. */
export function presenceToWire(presence: UserPresence): Record<string, unknown> {
  const wire: Record<string, unknown> = {};
  for (const [key, name] of WIRE_FIELDS) {
    const value = presence[key];
    // A range travels as a list, and an absent selection or pin as null: a
    // consumer should not have to tell absent from unset.
    wire[name] = key === "scrollRange" ? [...(value as [number, number])] : (value ?? null);
  }
  return wire;
}

/**
 * Measure a value the way the reference does.
 *
 * `json.dumps(value, default=str)` with CPython's spaced separators — the
 * bound is on that encoding, so a compact one would allow a value the
 * reference refuses.
 */
function encodedLength(value: unknown): number {
  // The separators matter — a compact encoding is shorter and would let a
  // value through that the reference refuses. The key order does not, since
  // sorting cannot change a length, but it is left as the reference has it so
  // the two calls read the same.
  return pyJsonDumps(stringifyUnsupported(value), { sortKeys: false, separators: [", ", ": "] }).length;
}

/** Replace anything JSON cannot carry with its string form, as `default=str`. */
function stringifyUnsupported(value: unknown): unknown {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(stringifyUnsupported);
  }
  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, stringifyUnsupported(item)]));
  }
  return String(value);
}

/**
 * Check an untrusted `selection` or `pin`.
 *
 * @throws {Error} When it is neither absent nor a plain object, carries more
 *   keys than {@link MAX_PRESENCE_DICT_KEYS}, or encodes larger than
 *   {@link MAX_PRESENCE_DICT_BYTES}.
 */
export function validatePresenceDict(field: string, value: unknown): void {
  // Absent clears the field, which is a legitimate thing for a browser to do.
  if (value === undefined || value === null) {
    return;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`invalid presence ${field}: must be a dict or None`);
  }
  const keys = Object.keys(value as Record<string, unknown>).length;
  if (keys > MAX_PRESENCE_DICT_KEYS) {
    throw new Error(`invalid presence ${field}: too many keys (${keys} > ${MAX_PRESENCE_DICT_KEYS})`);
  }
  const encoded = encodedLength(value);
  if (encoded > MAX_PRESENCE_DICT_BYTES) {
    throw new Error(`invalid presence ${field}: too large (${encoded} > ${MAX_PRESENCE_DICT_BYTES} bytes)`);
  }
}

/** Options for {@link PresenceStore}. */
export interface PresenceStoreOptions {
  /** Wall clock in seconds. */
  now?: () => number;
}

/** Who is in a session, and what they are doing. */
export class PresenceStore {
  readonly #users = new Map<string, UserPresence>();
  readonly #now: () => number;

  constructor(options: PresenceStoreOptions = {}) {
    this.#now = options.now ?? (() => Date.now() / 1000);
  }

  /** How many people are here. */
  get count(): number {
    return this.#users.size;
  }

  /** Add somebody. */
  add(userId: string, name: string, color: string, role: string, initials = ""): UserPresence {
    const presence: UserPresence = {
      userId,
      name,
      color,
      role,
      initials,
      scrollLine: 0,
      scrollRange: [0, 0],
      totalLines: 0,
      selection: undefined,
      pin: undefined,
      typing: false,
      queuedKeys: "",
      cols: 0,
      rows: 0,
      lastActivityAt: this.#now(),
      isOwner: false,
    };
    this.#users.set(userId, presence);
    return presence;
  }

  /**
   * Change somebody's presence.
   *
   * Every field is checked before any is written, so a rejected value leaves
   * the stored user exactly as it was rather than half-updated.
   *
   * @returns The updated presence, or nothing when there is no such user.
   * @throws {Error} On a field the record does not have, or an untrusted
   *   value that fails {@link validatePresenceDict}.
   */
  update(userId: string, fields: Record<string, unknown>): UserPresence | undefined {
    const presence = this.#users.get(userId);
    if (presence === undefined) {
      return undefined;
    }
    for (const [name, value] of Object.entries(fields)) {
      if (!Object.hasOwn(FIELD_NAMES, name)) {
        throw new Error(`Unknown presence field: ${name}`);
      }
      if (VALIDATED.has(name)) {
        validatePresenceDict(name, value);
      }
    }
    for (const [name, value] of Object.entries(fields)) {
      (presence as unknown as Record<string, unknown>)[FIELD_NAMES[name] as string] = value;
    }
    presence.lastActivityAt = this.#now();
    return presence;
  }

  /** Forget somebody. */
  remove(userId: string): UserPresence | undefined {
    const presence = this.#users.get(userId);
    this.#users.delete(userId);
    return presence;
  }

  /** One person, if they are here. */
  get(userId: string): UserPresence | undefined {
    return this.#users.get(userId);
  }

  /** Everybody, in the order they arrived. */
  getAll(): UserPresence[] {
    return [...this.#users.values()];
  }

  /** Whoever holds control, if anybody does. */
  getOwner(): UserPresence | undefined {
    for (const presence of this.#users.values()) {
      if (presence.isOwner) {
        return presence;
      }
    }
    return undefined;
  }

  /**
   * Give control to somebody.
   *
   * Clears the rest in the same pass: two people both believing they hold
   * control is the failure this prevents.
   */
  setOwner(userId: string): void {
    for (const presence of this.#users.values()) {
      presence.isOwner = presence.userId === userId;
    }
  }

  /** Leave nobody in control. */
  clearOwner(): void {
    for (const presence of this.#users.values()) {
      presence.isOwner = false;
    }
  }

  /** Whether somebody has been quiet for longer than `thresholdS`. */
  isIdle(presence: UserPresence, thresholdS: number): boolean {
    // Strictly longer: somebody exactly at the threshold has not passed it.
    return this.#now() - presence.lastActivityAt > thresholdS;
  }

  /** Forget everybody who has been quiet too long, naming them. */
  pruneIdle(thresholdS: number): string[] {
    const stale = [...this.#users.values()]
      .filter((presence) => this.isIdle(presence, thresholdS))
      .map((presence) => presence.userId);
    for (const userId of stale) {
      this.#users.delete(userId);
    }
    return stale;
  }

  /** The message a joiner is sent. */
  getSyncPayload(config: Record<string, unknown>): Record<string, unknown> {
    return makePresenceSync(this.getAll().map(presenceToWire), config);
  }

  /** The colours already in use, so a new joiner gets a distinct one. */
  takenColors(): ReadonlySet<string> {
    return new Set([...this.#users.values()].map((presence) => presence.color));
  }
}
