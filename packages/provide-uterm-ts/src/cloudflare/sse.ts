//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The polling server-sent-events endpoint for a Durable Object session.
 *
 * Port of the Python module `provide.uterm.cloudflare.do._sse`.
 *
 * A Durable Object is evicted when idle and cannot hold a connection open
 * across that, so events are delivered by polling: each request returns the
 * events since a position and closes, and the browser reconnects. The `id:`
 * line is what makes it work — `EventSource` echoes the last one it saw in
 * `Last-Event-ID`, so the client carries its own position across the gap.
 */

import { pyInt, pyJsonDumps } from "../pycompat/index.ts";

/** How long the browser waits before reconnecting, in milliseconds. */
export const SSE_RETRY_MS = 3000;

/** How many events one response may carry. */
export const SSE_MAX_EVENTS = 100;

/** The query parameter carrying the client's position. */
const POSITION_PARAM = "after_seq";

/** The header `EventSource` sends it in instead, on reconnect. */
const POSITION_HEADER = "last-event-id";

/**
 * How the reference serialises an event.
 *
 * A plain `json.dumps` call: fields in the order they were written, non-ASCII
 * escaped, and a space after each separator. None of that is required by SSE
 * — both ends read the field as JSON — but the two encoders disagree by
 * default, and the bodies have to match byte for byte.
 */
const EVENT_JSON = { sortKeys: false, separators: [", ", ": "] } as const;

/** Where the events come from. */
export interface SseEventSource {
  listEventsSince(workerId: string, afterSeq: number, limit: number): readonly object[];
}

/** What the route reads off the session runtime. */
export interface SseRuntime {
  /** The session this Durable Object is. */
  workerId: string;
  store: SseEventSource;
}

/** A request, which may or may not carry headers. */
export interface SseRequest {
  headers?: { get(name: string): string | null | undefined } | undefined;
}

/** Options for {@link buildSseResponse}. */
export interface SseResponseOptions {
  /** How long the browser waits before reconnecting. */
  retryMs?: number;
}

/** Read one field off an event row. */
function field(event: object, name: string): unknown {
  // The store hands back dictionaries; the reference reads them as such.
  return (event as Record<string, unknown>)[name];
}

/**
 * The first value a query string gives for one parameter.
 *
 * Hand-parsed rather than handed to `URL`, which needs an absolute address —
 * the route is given whatever the caller had, and that is not always one.
 * Blank values are skipped, matching `parse_qs`, so `?after_seq=` falls
 * through to the header rather than reading as a position of nothing.
 */
function queryValue(url: string, name: string): string | undefined {
  // The fragment goes first. Everything after the marker belongs to it, so a
  // question mark inside one never starts a query — looking for the marker
  // only after the question mark would read `#frag?after_seq=9` as a
  // position.
  const marker = url.indexOf("#");
  const addressed = marker === -1 ? url : url.slice(0, marker);
  const start = addressed.indexOf("?");
  // No query at all. An early return rather than an empty one: `slice` on a
  // missing index would take the whole address as the query, which happens to
  // find nothing for any real url but only by accident.
  if (start === -1) {
    return undefined;
  }
  for (const pair of addressed.slice(start + 1).split("&")) {
    // The *first* equals sign, so `after_seq=5=6` is this parameter carrying
    // an unreadable value rather than a differently-named one — which decides
    // whether a second occurrence gets a turn.
    const split = pair.indexOf("=");
    // A bare name carries no value, which `parse_qs` drops along with the
    // blank ones. Dropped rather than read: taking the whole pair as its own
    // value would match `after_seqs` against a name one character shorter and
    // stop on it, never reaching the real parameter behind it.
    if (split === -1) {
      continue;
    }
    const value = pair.slice(split + 1);
    if (value === "" || decodeURIComponent(pair.slice(0, split).replaceAll("+", " ")) !== name) {
      continue;
    }
    // The first wins: a repeated parameter is one client sending two
    // positions, and the earlier one is the one it has certainly seen.
    return decodeURIComponent(value.replaceAll("+", " "));
  }
  return undefined;
}

/**
 * Where the client says it got to.
 *
 * The parameter is what this request asked for; the header is only where the
 * last one ended, so the parameter wins. A position that cannot be read is
 * the beginning rather than a refusal — replaying events a client has already
 * seen is recoverable, since they carry their own sequence numbers, while
 * refusing would leave it with no stream at all.
 */
function readPosition(request: SseRequest, url: string): number {
  // The reference substitutes "0" for an absent or empty header. Left out
  // rather than mirrored: an unreadable position is already the beginning, so
  // the substitution cannot change any answer.
  const raw = queryValue(url, POSITION_PARAM) ?? request.headers?.get(POSITION_HEADER) ?? "";
  // Clamped rather than passed through: the store's `seq > ?` would accept a
  // negative and return everything, which is the same answer, but only by
  // accident of the comparison.
  return Math.max(0, pyInt(raw) ?? 0);
}

/**
 * Build the response body for a batch of events.
 *
 * Each event is an `id:` line and a `data:` line, terminated by a blank one —
 * without which the two `data:` lines would be read as a single event. The
 * trailing `retry:` tells the browser when to come back, which matters
 * because the connection closes after every batch.
 */
export function buildSseResponse(events: readonly object[], options: SseResponseOptions = {}): Response {
  const lines: string[] = [];
  for (const event of events) {
    // An event with no sequence still gets an id line, empty: the client's
    // position simply does not advance.
    lines.push(`id: ${field(event, "seq") ?? ""}`);
    lines.push(`data: ${pyJsonDumps(event, EVENT_JSON)}`);
    lines.push("");
  }
  lines.push(`retry: ${options.retryMs ?? SSE_RETRY_MS}`);
  lines.push("");
  return new Response(lines.join("\n"), {
    status: 200,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache",
      // A proxy holding the batch back would defeat the point of sending it.
      "x-accel-buffering": "no",
    },
  });
}

/** Handle `GET /api/sessions/{id}/events/stream`. */
export async function routeSse(
  runtime: SseRuntime,
  request: SseRequest,
  url: string,
  sessionId: string,
): Promise<Response> {
  if (sessionId !== runtime.workerId) {
    // Refused as JSON rather than as a stream: an `EventSource` would
    // otherwise try to parse the refusal as events. The store is not touched,
    // because reaching it would mean a mismatched id had already read
    // something.
    return new Response(pyJsonDumps({ error: "not_found" }, EVENT_JSON), {
      status: 404,
      headers: { "content-type": "application/json" },
    });
  }
  // The object's own session, not the one in the path. The check above has
  // already proved the two equal, so this cannot change an answer — it says
  // which of them is authoritative, and a mismatch cannot read another
  // session's log even if that check ever moved.
  const events = runtime.store.listEventsSince(runtime.workerId, readPosition(request, url), SSE_MAX_EVENTS);
  return buildSseResponse(events);
}
