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

import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
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
 */
export interface RecordingStore {
  startSession(sessionId: string, metadata: Record<string, unknown>): Promise<void>;
  appendEvents(sessionId: string, events: readonly RecordingEvent[]): Promise<void>;
  endSession(sessionId: string): Promise<void>;
  recordingMeta(sessionId: string): Promise<RecordingMeta>;
  getEntries(sessionId: string, options?: GetEntriesOptions): Promise<RecordingEvent[]>;
  getPath(sessionId: string): Promise<string | null>;
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

  startSession(sessionId: string, metadata: Record<string, unknown>): Promise<void> {
    const handle = this.#handle(sessionId);
    handle.writeSync(`${JSON.stringify(startEvent(sessionId, metadata))}\n`);
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

  startSession(sessionId: string, metadata: Record<string, unknown>): Promise<void> {
    this.#sessions.set(sessionId, { metadata, active: true });
    this.#list(sessionId).push(startEvent(sessionId, metadata));
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
    const sizeBytes = events.reduce(
      (total, event) => total + pyJsonDumps(event, { sortKeys: false, separators: [", ", ": "] }).length + 1,
      0,
    );
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
