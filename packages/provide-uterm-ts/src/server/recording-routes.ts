//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Reading a session's recording back over HTTP.
 *
 * Port of the reference's `sessions.recording`, `sessions.recording_entries`
 * and `sessions.recording_download` handlers (`routes/sessions.py`) and the
 * registry methods behind them (`recording_meta`, `recording_entries`,
 * `recording_path`).
 *
 * The order of refusals is the reference's, and each is a contract a client
 * can observe: authentication first (the app does that), then the query
 * parameters — so an unknown session asked a bad question is a 422, not a
 * 404 — then whether the session exists, then whether this caller may read
 * its recording. Reading a recording needs both what reading the session
 * needs and the `session.recording.read` capability, so a scoped token that
 * kept `session.read` alone sees the session and not what it recorded.
 *
 * What a running session has buffered is flushed before the meta and the
 * entries are answered, as the reference flushes it, so a reader sees what was
 * just typed rather than what the last batch happened to hold.
 */

import { realpathSync } from "node:fs";
import { basename, relative, sep } from "node:path";
import type { RecordingStore } from "../recording/index.ts";
import type { ServerPrincipal } from "../serverauth/index.ts";
import { canReadSession, hasCapability } from "./authorization.ts";
import { fileResponse } from "./file-response.ts";
import { checkInt, checkString, lastQueryValue, type QueryError, validationFailure } from "./query-params.ts";
import type { RouteHandler } from "./route-binding.ts";
import type { SessionRegistry } from "./session-registry.ts";
import type { SessionDefinition, SessionRuntimeStatus } from "./session-status.ts";

/** Where recordings are, and how to make what is buffered readable. */
export interface RecordingAccess {
  /** The store sessions record to. */
  readonly recordingStore: RecordingStore;
  /** Where a local store writes, which a download must resolve inside. */
  readonly recordingDirectory: string;
  /** Write out what a session's recording has buffered. */
  flushRecording(sessionId: string): Promise<void>;
}

/** What the handlers are built over. */
export interface RecordingRouteDeps {
  registry: SessionRegistry;
  recordings: RecordingAccess;
  principal: ServerPrincipal;
  request: Request;
  url: URL;
}

/** The capability that reading a recording needs beyond reading its session. */
export const RECORDING_READ_CAPABILITY = "session.recording.read";

/** The reference's `can_read_recording`. */
export function canReadRecording(principal: ServerPrincipal, session: SessionDefinition): boolean {
  return canReadSession(principal, session) && hasCapability(principal, RECORDING_READ_CAPABILITY);
}

/** One refusal, in the shape the reference's framework emits. */
function refusal(status: number, detail: string): Response {
  return Response.json({ detail }, { status });
}

/** The page size when none is asked for, the reference's `Query` default. */
export const DEFAULT_ENTRIES_LIMIT = 200;

/** The entries query, or every way it fails. */
interface EntriesQuery {
  limit: number;
  offset: number | null;
  event: string | null;
}

/**
 * Validate `limit`, `offset` and `event` as the reference's `Query(...)`
 * declarations do: `limit` in 1..500 defaulting to 200, `offset` at least 0
 * and otherwise absent, `event` at most 100 characters. Every failure is
 * reported, in declaration order.
 */
export function parseEntriesQuery(parameters: URLSearchParams): EntriesQuery | QueryError[] {
  const errors: QueryError[] = [];
  const query: EntriesQuery = { limit: DEFAULT_ENTRIES_LIMIT, offset: null, event: null };
  const limit = lastQueryValue(parameters, "limit");
  if (limit !== null) {
    const checked = checkInt("limit", limit, { ge: 1, le: 500 });
    if (checked.ok) {
      query.limit = checked.value;
    } else {
      errors.push(checked.error);
    }
  }
  const offset = lastQueryValue(parameters, "offset");
  if (offset !== null) {
    const checked = checkInt("offset", offset, { ge: 0 });
    if (checked.ok) {
      query.offset = checked.value;
    } else {
      errors.push(checked.error);
    }
  }
  const event = lastQueryValue(parameters, "event");
  if (event !== null) {
    const checked = checkString("event", event, 100);
    if (checked.ok) {
      // The reference's stores filter on `if event and ...`: an empty filter
      // is no filter.
      query.event = checked.value === "" ? null : checked.value;
    } else {
      errors.push(checked.error);
    }
  }
  return errors.length > 0 ? errors : query;
}

/**
 * Whether `path` exists and resolves inside `directory`, symbolic links
 * followed. A path that does not resolve — missing, or no path at all — is
 * not inside anything.
 */
function resolvesInside(path: string | null, directory: string): boolean {
  let real: string;
  let root: string;
  try {
    // No path at all becomes one no filesystem accepts — a NUL byte — rather
    // than "", which Node resolves to the working directory.
    real = realpathSync(path ?? "\0");
    root = realpathSync(directory);
  } catch {
    return false;
  }
  // Inside, or the directory itself, as `Path.is_relative_to` answers: the
  // relative path never starts by climbing out.
  return relative(root, real).split(sep)[0] !== "..";
}

/** The three handlers, by capability. */
export function recordingHandlers(deps: RecordingRouteDeps): ReadonlyMap<string, RouteHandler> {
  const { registry, recordings, principal, request, url } = deps;

  /** The session a route names, or the refusal for why it cannot be read. */
  function readable(sessionId: string): SessionDefinition | Response {
    const definition = registry.definition(sessionId);
    if (definition === undefined) {
      return refusal(404, `unknown session: ${sessionId}`);
    }
    if (!canReadRecording(principal, definition)) {
      return refusal(403, "insufficient privileges");
    }
    return definition;
  }

  return new Map<string, RouteHandler>([
    [
      "sessions.recording",
      async (context) => {
        const sessionId = context.params.session_id as string;
        const definition = readable(sessionId);
        if (definition instanceof Response) {
          return definition;
        }
        // Present, because its definition is.
        const enabled = (registry.status(sessionId) as SessionRuntimeStatus).recording_enabled;
        await recordings.flushRecording(sessionId);
        const meta = await recordings.recordingStore.recordingMeta(sessionId);
        return Response.json({ ...meta, enabled });
      },
    ],
    [
      "sessions.recording_entries",
      async (context) => {
        const query = parseEntriesQuery(url.searchParams);
        if (Array.isArray(query)) {
          return validationFailure(query);
        }
        const sessionId = context.params.session_id as string;
        const definition = readable(sessionId);
        if (definition instanceof Response) {
          return definition;
        }
        await recordings.flushRecording(sessionId);
        return Response.json(await recordings.recordingStore.getEntries(sessionId, query));
      },
    ],
    [
      "sessions.recording_download",
      async (context) => {
        const sessionId = context.params.session_id as string;
        const definition = readable(sessionId);
        if (definition instanceof Response) {
          return definition;
        }
        const path = await recordings.recordingStore.getPath(sessionId);
        // A file that has gone, and one that resolves outside the recording
        // directory — a symbolic link planted there — are the same answer.
        if (!resolvesInside(path, recordings.recordingDirectory)) {
          return refusal(404, "recording not available");
        }
        return fileResponse(path as string, {
          filename: basename(path as string),
          mediaType: "application/json",
          requestHeaders: request.headers,
        });
      },
    ],
  ]);
}
