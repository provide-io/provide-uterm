//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Recording-store backends for terminal session capture.
 *
 * Defines the {@link RecordingStore} contract plus three reference
 * implementations: a JSONL file store, an in-memory store, and a no-op store
 * for when recording is disabled.
 *
 * Port of the Python module `provide.uterm.recording` and the Go package
 * `recording`.
 */

import { randomBytes } from "node:crypto";
import {
  closeSync,
  constants,
  existsSync,
  lstatSync,
  openSync,
  readFileSync,
  readSync,
  renameSync,
  statSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { type AppendHandle, secureOpenAppend } from "../file-io/index.ts";
import { pyJsonDumps } from "../pycompat/index.ts";

/** One recorded event. At minimum it carries `ts`, `event` and `data`. */
export type RecordingEvent = Record<string, unknown>;

/** What a recording's metadata query returns. */
export interface RecordingMeta {
  session_id: string;
  exists: boolean;
  size_bytes: number;
  [key: string]: unknown;
}

/** Query shape for {@link RecordingStore.getEntries}. */
export interface GetEntriesOptions {
  /** Maximum events to return. Zero means the default of 200. */
  limit?: number;
  /** Events to skip from the start. Absent means "return the tail instead". */
  offset?: number | null;
  /** Only include events of this type. */
  event?: string | null;
}

/**
 * Persists and retrieves session recordings.
 *
 * The lifecycle is `startSession` once, `appendEvents` repeatedly, then
 * `endSession` once. The query methods may be called at any time, including
 * while the session is still active.
 *
 * A session id can be reused by a later, unrelated session, so a store must
 * never let one session instance read another's entries: `startSession`
 * continues the recording held for the id only when `metadata.instance` (see
 * {@link RECORDING_INSTANCE_KEY}) names the instance that started it (a
 * reconnect), and otherwise -- a different instance, or none named -- begins
 * afresh, keeping or dropping the old recording but never serving it under
 * the id again.
 */
export interface RecordingStore {
  startSession(sessionId: string, metadata: Record<string, unknown>): Promise<void>;
  appendEvents(sessionId: string, events: readonly RecordingEvent[]): Promise<void>;
  endSession(sessionId: string): Promise<void>;
  recordingMeta(sessionId: string): Promise<RecordingMeta>;
  getEntries(sessionId: string, options?: GetEntriesOptions): Promise<RecordingEvent[]>;
  getPath(sessionId: string): Promise<string | null>;
  /**
   * Retire the session's recording: afterwards nothing is served for the id
   * until a new recording starts. Called when a session goes away (and before
   * one is created), so the id's next holder is never served this one's
   * entries. Optional, so a store written before it existed still satisfies
   * the contract and keeps only the `startSession` instance rule.
   */
  retireSession?(sessionId: string): Promise<void>;
}

/** Retire `sessionId`'s recording in `store` when the store can. Port of `retire_recording`. */
export async function retireRecording(store: RecordingStore, sessionId: string): Promise<void> {
  await store.retireSession?.(sessionId);
}

/**
 * How long CPython's `json.dumps` makes a record, as it will be stored.
 *
 * Default separators and ASCII escapes, which is what the reference measures
 * sizes and quotas with. The record is first put through the same JSON
 * round trip a store's write applies, so a field that is `undefined` is
 * dropped rather than refused: it never reaches disk, so it has no size.
 */
export function pyJsonSize(record: unknown): number {
  // Key order cannot change a length, so the sort pyJsonDumps defaults to is
  // left in place.
  return pyJsonDumps(JSON.parse(JSON.stringify(record)), { separators: [", ", ": "] }).length;
}

/**
 * Metadata key naming the session instance a recording belongs to. Port of the
 * reference's `RECORDING_INSTANCE_KEY`.
 */
export const RECORDING_INSTANCE_KEY = "instance";

/** How much of a recording's first line is read to find its instance. */
const FIRST_LINE_LIMIT = 64 * 1024;

/** The instance `startSession` metadata names, or null when it names none. */
export function recordingInstance(metadata: Readonly<Record<string, unknown>> | undefined): string | null {
  const value = metadata?.[RECORDING_INSTANCE_KEY];
  return typeof value === "string" && value !== "" ? value : null;
}

/**
 * The instance that started the recording at `path`, or null if it cannot say.
 *
 * Opened without following a symlink: a link planted at the path is not a
 * recording this store wrote, and null retires it like any other stranger.
 */
function instanceOnFile(path: string): string | null {
  let fd: number;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  } catch {
    return null;
  }
  const buffer = Buffer.alloc(FIRST_LINE_LIMIT);
  let length: number;
  try {
    length = readSync(fd, buffer, 0, FIRST_LINE_LIMIT, 0);
  } catch {
    return null;
  } finally {
    closeSync(fd);
  }
  const text = buffer.subarray(0, length).toString("utf-8");
  const newline = text.indexOf("\n");
  let start: unknown;
  try {
    start = JSON.parse(newline === -1 ? text : text.slice(0, newline));
  } catch {
    return null;
  }
  if (typeof start !== "object" || start === null || (start as RecordingEvent).event !== "log_start") {
    return null;
  }
  const data = (start as RecordingEvent).data;
  return typeof data === "object" && data !== null ? recordingInstance(data as Record<string, unknown>) : null;
}

/**
 * A unique name to move a superseded recording aside to.
 *
 * It keeps the `.jsonl` suffix, so the retention sweep still ages it out, and
 * puts a `.` before it that no session id may contain, so no session's
 * recording path can ever name it. Port of `retired_recording_name`.
 */
export function retiredRecordingName(sessionId: string): string {
  return `${sessionId}.${BigInt(Date.now()) * 1_000_000n}-${randomBytes(4).toString("hex")}.jsonl`;
}

/** Move the recording at `path` aside unless `instance` started it. */
function retireForeignRecording(path: string, sessionId: string, instance: string | null): void {
  try {
    lstatSync(path);
  } catch {
    return;
  }
  if (instance !== null && instanceOnFile(path) === instance) {
    return;
  }
  renameSync(path, join(dirname(path), retiredRecordingName(sessionId)));
}

/** Default page size when a caller passes zero. */
const DEFAULT_LIMIT = 200;
/** Hard ceiling on a page, for parity with the Go and C# stores. */
const MAX_LIMIT = 500;

/**
 * Clamp a page size to 1..500, treating zero as the default of 200.
 *
 * Zero is special rather than empty because a caller that omits the limit
 * arrives here as zero through several call paths, and returning nothing
 * would silently drop a page.
 */
export function normalizeLimit(limit: number): number {
  const requested = limit === 0 ? DEFAULT_LIMIT : limit;
  return Math.max(1, Math.min(requested, MAX_LIMIT));
}

/** Whether an event passes the optional type filter. */
function matchesEvent(entry: RecordingEvent, event: string | null | undefined): boolean {
  return event === null || event === undefined || entry.event === event;
}

/**
 * Page a list of already-filtered events.
 *
 * With no offset this returns the *tail*, which is what a live viewer wants;
 * with an offset it pages forward from the start. A negative offset skips
 * nothing rather than counting from the end.
 */
function page(entries: readonly RecordingEvent[], limit: number, offset: number | null | undefined): RecordingEvent[] {
  const normalized = normalizeLimit(limit);
  if (offset === null || offset === undefined) {
    return entries.slice(-normalized);
  }
  const start = Math.max(0, offset);
  return entries.slice(start, start + normalized);
}

/** Build the automatic session-open event. */
function startEvent(sessionId: string, metadata: Record<string, unknown>): RecordingEvent {
  return { ts: Date.now() / 1000, event: "log_start", data: metadata, session_id: sessionId };
}

/** Build the automatic session-close event. */
function stopEvent(sessionId: string): RecordingEvent {
  return { ts: Date.now() / 1000, event: "log_stop", data: {}, session_id: sessionId };
}

/** File-backed store writing one JSON object per line. */
export class LocalFileRecordingStore implements RecordingStore {
  readonly #directory: string;
  readonly #files = new Map<string, AppendHandle>();

  constructor(directory: string) {
    this.#directory = directory;
  }

  /** The JSONL path a session records to. */
  #path(sessionId: string): string {
    return join(this.#directory, `${sessionId}.jsonl`);
  }

  /** The open handle for a session, opening one if needed. */
  #handle(sessionId: string): AppendHandle {
    const existing = this.#files.get(sessionId);
    if (existing !== undefined) {
      return existing;
    }
    // The sink is opened symlink-safe and owner-only; see file-io.
    const handle = secureOpenAppend(this.#path(sessionId));
    this.#files.set(sessionId, handle);
    return handle;
  }

  /**
   * Open the session's recording and write its `log_start`.
   *
   * A handle an earlier start left open is closed first, and a recording on
   * file that another instance started is moved aside (see
   * {@link RecordingStore}): whether this start continues it is decided from
   * the file alone.
   */
  startSession(sessionId: string, metadata: Record<string, unknown>): Promise<void> {
    const stale = this.#files.get(sessionId);
    if (stale !== undefined) {
      this.#files.delete(sessionId);
      stale.close();
    }
    retireForeignRecording(this.#path(sessionId), sessionId, recordingInstance(metadata));
    const handle = this.#handle(sessionId);
    handle.writeSync(`${JSON.stringify(startEvent(sessionId, metadata))}\n`);
    return Promise.resolve();
  }

  /**
   * Move the session's recording aside: kept for retention, served to nobody.
   * A handle still open for it is closed first, without a `log_stop`.
   */
  retireSession(sessionId: string): Promise<void> {
    const stale = this.#files.get(sessionId);
    if (stale !== undefined) {
      this.#files.delete(sessionId);
      stale.close();
    }
    retireForeignRecording(this.#path(sessionId), sessionId, null);
    return Promise.resolve();
  }

  appendEvents(sessionId: string, events: readonly RecordingEvent[]): Promise<void> {
    const handle = this.#handle(sessionId);
    for (const event of events) {
      handle.writeSync(`${JSON.stringify(event)}\n`);
    }
    return Promise.resolve();
  }

  endSession(sessionId: string): Promise<void> {
    const handle = this.#files.get(sessionId);
    if (handle !== undefined) {
      this.#files.delete(sessionId);
      handle.writeSync(`${JSON.stringify(stopEvent(sessionId))}\n`);
      handle.close();
    }
    return Promise.resolve();
  }

  recordingMeta(sessionId: string): Promise<RecordingMeta> {
    const path = this.#path(sessionId);
    const exists = existsSync(path);
    return Promise.resolve({
      session_id: sessionId,
      exists,
      path: exists ? path : null,
      size_bytes: exists ? statSync(path).size : 0,
    });
  }

  /**
   * Read a page of events.
   *
   * A line that is not valid JSON is skipped rather than failing the read: a
   * recording truncated by a crash is still worth serving.
   */
  getEntries(sessionId: string, options: GetEntriesOptions = {}): Promise<RecordingEvent[]> {
    const path = this.#path(sessionId);
    if (!existsSync(path)) {
      return Promise.resolve([]);
    }
    const matching: RecordingEvent[] = [];
    for (const line of readFileSync(path, "utf-8").split("\n")) {
      if (line === "") {
        continue;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(line);
      } catch {
        continue;
      }
      const entry = parsed as RecordingEvent;
      if (matchesEvent(entry, options.event)) {
        matching.push(entry);
      }
    }
    return Promise.resolve(page(matching, options.limit ?? DEFAULT_LIMIT, options.offset));
  }

  getPath(sessionId: string): Promise<string | null> {
    const path = this.#path(sessionId);
    return Promise.resolve(existsSync(path) ? path : null);
  }
}

/** In-memory store, useful for tests and as a reference implementation. */
export class InMemoryRecordingStore implements RecordingStore {
  readonly #sessions = new Map<string, { metadata: Record<string, unknown>; active: boolean }>();
  readonly #events = new Map<string, RecordingEvent[]>();

  /** The event list for a session, creating it if needed. */
  #list(sessionId: string): RecordingEvent[] {
    let events = this.#events.get(sessionId);
    if (events === undefined) {
      events = [];
      this.#events.set(sessionId, events);
    }
    return events;
  }

  /** Record `log_start`, first dropping a recording another instance started. */
  startSession(sessionId: string, metadata: Record<string, unknown>): Promise<void> {
    const instance = recordingInstance(metadata);
    const previous = this.#sessions.get(sessionId);
    if (instance === null || previous === undefined || recordingInstance(previous.metadata) !== instance) {
      this.#events.set(sessionId, []);
    }
    this.#sessions.set(sessionId, { metadata, active: true });
    this.#list(sessionId).push(startEvent(sessionId, metadata));
    return Promise.resolve();
  }

  /** Drop the session's recording: no later holder of the id is served it. */
  retireSession(sessionId: string): Promise<void> {
    this.#sessions.delete(sessionId);
    this.#events.delete(sessionId);
    return Promise.resolve();
  }

  appendEvents(sessionId: string, events: readonly RecordingEvent[]): Promise<void> {
    this.#list(sessionId).push(...events);
    return Promise.resolve();
  }

  endSession(sessionId: string): Promise<void> {
    this.#list(sessionId).push(stopEvent(sessionId));
    const session = this.#sessions.get(sessionId);
    if (session !== undefined) {
      session.active = false;
    }
    return Promise.resolve();
  }

  /**
   * Report the recording's size.
   *
   * The size is the serialised length plus one byte per event for its
   * newline, measured with CPython's default JSON separators so the number
   * matches what the reference reports for the same events.
   */
  recordingMeta(sessionId: string): Promise<RecordingMeta> {
    const events = this.#events.get(sessionId) ?? [];
    const sizeBytes = events.reduce((total, event) => total + pyJsonSize(event) + 1, 0);
    return Promise.resolve({ session_id: sessionId, exists: events.length > 0, size_bytes: sizeBytes });
  }

  getEntries(sessionId: string, options: GetEntriesOptions = {}): Promise<RecordingEvent[]> {
    const all = this.#events.get(sessionId) ?? [];
    const matching = all.filter((entry) => matchesEvent(entry, options.event));
    return Promise.resolve(page(matching, options.limit ?? DEFAULT_LIMIT, options.offset));
  }

  getPath(_sessionId: string): Promise<string | null> {
    return Promise.resolve(null);
  }
}

/**
 * No-op store for when recording is disabled.
 *
 * Writes are discarded and reads are empty, which lets calling code keep one
 * code path instead of null-checking a store everywhere.
 */
export class NullRecordingStore implements RecordingStore {
  startSession(_sessionId: string, _metadata: Record<string, unknown>): Promise<void> {
    return Promise.resolve();
  }

  appendEvents(_sessionId: string, _events: readonly RecordingEvent[]): Promise<void> {
    return Promise.resolve();
  }

  endSession(_sessionId: string): Promise<void> {
    return Promise.resolve();
  }

  recordingMeta(sessionId: string): Promise<RecordingMeta> {
    return Promise.resolve({ session_id: sessionId, exists: false, size_bytes: 0 });
  }

  getEntries(_sessionId: string, _options: GetEntriesOptions = {}): Promise<RecordingEvent[]> {
    return Promise.resolve([]);
  }

  getPath(_sessionId: string): Promise<string | null> {
    return Promise.resolve(null);
  }
}
