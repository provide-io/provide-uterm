//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The sessions a server knows about, and how a list of them is narrowed.
 *
 * Port of the read half of `provide.uterm.server.registry.SessionRegistry`
 * together with the filtering, sorting and paging that
 * `sessions.list` applies on top of it.
 *
 * The registry holds definitions and the runtime state each one is in. What it
 * does *not* do here is run a connector: this is the store the HTTP surface
 * reads, and starting a session is a separate concern with a separate seam.
 */

import {
  INITIAL_RUNTIME_STATE,
  type SessionDefinition,
  type SessionRuntimeState,
  type SessionRuntimeStatus,
  sessionRuntimeStatus,
} from "./session-status.ts";

/** What `GET /api/sessions` accepts, with the reference's own defaults. */
export interface SessionListQuery {
  tag?: readonly string[] | undefined;
  connector_type?: string | undefined;
  visibility?: string | undefined;
  state?: string | undefined;
  q?: string | undefined;
  sort?: string | undefined;
  order?: string | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}

/** The fields a list may be sorted by. Anything else falls back to the first. */
const SORTABLE = new Set(["created_at", "display_name", "session_id"]);

/**
 * How a session's status is read for sorting.
 *
 * Only ever one of {@link SORTABLE}, every one of which is a string on every
 * status — so there is nothing to stand in for.
 */
function sortKey(status: SessionRuntimeStatus, field: string): string {
  return String(status[field as keyof SessionRuntimeStatus]);
}

/** Whether a status matches a free-text query, which is matched case-blind. */
function matchesText(status: SessionRuntimeStatus, needle: string): boolean {
  const lowered = needle.toLowerCase();
  return (
    status.session_id.toLowerCase().includes(lowered) ||
    status.display_name.toLowerCase().includes(lowered) ||
    status.tags.some((tag) => tag.toLowerCase().includes(lowered))
  );
}

/**
 * Narrow, order and page a list of sessions.
 *
 * Exported on its own because it is the part with rules: the registry only
 * holds things.
 */
export function filterSessions(
  sessions: readonly SessionRuntimeStatus[],
  query: SessionListQuery = {},
): SessionRuntimeStatus[] {
  let results = [...sessions];
  if (query.tag !== undefined && query.tag.length > 0) {
    const wanted = new Set(query.tag);
    results = results.filter((status) => status.tags.some((tag) => wanted.has(tag)));
  }
  if (query.connector_type) {
    results = results.filter((status) => status.connector_type === query.connector_type);
  }
  if (query.visibility) {
    results = results.filter((status) => status.visibility === query.visibility);
  }
  if (query.state) {
    results = results.filter((status) => status.lifecycle_state === query.state);
  }
  if (query.q) {
    const needle = query.q;
    results = results.filter((status) => matchesText(status, needle));
  }

  const field = query.sort !== undefined && SORTABLE.has(query.sort) ? query.sort : "created_at";
  // Anything that is not the word `asc` is descending, as the reference has
  // it — including a misspelling, which sorts rather than failing.
  const descending = query.order !== "asc";
  results.sort((left, right) => {
    const a = sortKey(left, field);
    const b = sortKey(right, field);
    // Equal keys compare as equal so the sort stays stable, which is what
    // keeps two servers with identical data answering in the same order.
    if (a === b) {
      return 0;
    }
    return (a < b ? -1 : 1) * (descending ? -1 : 1);
  });

  const offset = query.offset ?? 0;
  return results.slice(offset, offset + (query.limit ?? 50));
}

/** Every session a server was configured with, and the state each is in. */
export class SessionRegistry {
  readonly #definitions = new Map<string, SessionDefinition>();
  readonly #states = new Map<string, SessionRuntimeState>();
  readonly #recordingEnabledByDefault: boolean;

  constructor(definitions: readonly SessionDefinition[], recordingEnabledByDefault: boolean) {
    for (const definition of definitions) {
      this.#definitions.set(definition.session_id, definition);
      this.#states.set(definition.session_id, { ...INITIAL_RUNTIME_STATE });
    }
    this.#recordingEnabledByDefault = recordingEnabledByDefault;
  }

  /** How many sessions there are. What health reports as `active_sessions`. */
  get size(): number {
    return this.#definitions.size;
  }

  /** One session's definition, or nothing when no session has that id. */
  definition(sessionId: string): SessionDefinition | undefined {
    return this.#definitions.get(sessionId);
  }

  /** One session's status, or nothing when no session has that id. */
  status(sessionId: string): SessionRuntimeStatus | undefined {
    const definition = this.#definitions.get(sessionId);
    if (definition === undefined) {
      return undefined;
    }
    // Present for every definition, because both maps are filled together.
    const state = this.#states.get(sessionId) as SessionRuntimeState;
    return sessionRuntimeStatus(definition, state, this.#recordingEnabledByDefault);
  }

  /** Every session's definition, in configuration order. */
  definitions(): SessionDefinition[] {
    return [...this.#definitions.values()];
  }

  /** Every session's status, in configuration order. */
  statuses(): SessionRuntimeStatus[] {
    // Present for every definition, as above.
    return this.definitions().map((definition) => this.status(definition.session_id) as SessionRuntimeStatus);
  }

  /**
   * Move a session to a new runtime state.
   *
   * The seam a connector-driving layer reaches through. Nothing in the HTTP
   * read surface calls it, and a session nobody has started stays stopped —
   * which is what the reference reports until its own startup gets further.
   */
  setState(sessionId: string, state: Partial<SessionRuntimeState>): void {
    const current = this.#states.get(sessionId);
    if (current === undefined) {
      return;
    }
    this.#states.set(sessionId, { ...current, ...state });
  }
}
