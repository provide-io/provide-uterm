//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A session as the HTTP API reports it.
 *
 * Port of `SessionRuntimeStatus` in `provide.uterm.server.models` and of the
 * `SessionRuntime.status()` that fills it in.
 *
 * Every field here is one a client reads, and the live conformance matrix
 * compares the whole object field-for-field against the reference — so a
 * field this port dropped, renamed or spelt as absent rather than null is a
 * cell that fails, which is the point. `null` and *missing* are deliberately
 * different: a client can tell "has not stopped" from "this server does not
 * say" only if the first is `null` and the second is nothing at all.
 */

/** How far along a session is. */
export type SessionLifecycle = "stopped" | "starting" | "running" | "paused" | "error";

/** Whether a viewer may type into a session. */
export type InputMode = "open" | "hijack";

/** Who may see a session. */
export type Visibility = "public" | "operator" | "private";

/** A session as the configuration defines it, after defaults are filled in. */
export interface SessionDefinition {
  session_id: string;
  display_name: string;
  connector_type: string;
  connector_config: Readonly<Record<string, unknown>>;
  input_mode: InputMode;
  auto_start: boolean;
  tags: readonly string[];
  /** `null` defers to the recording configuration's own default. */
  recording_enabled: boolean | null;
  created_at: string;
  owner: string | null;
  visibility: Visibility;
}

/** What a running session knows about itself that its definition does not. */
export interface SessionRuntimeState {
  lifecycle_state: SessionLifecycle;
  connected: boolean;
  /** When it stopped, in seconds, or `null` while it has not. */
  stopped_at: number | null;
  last_error: string | null;
}

/** One session, in the shape `GET /api/sessions` answers with. */
export interface SessionRuntimeStatus {
  session_id: string;
  display_name: string;
  created_at: string;
  connector_type: string;
  lifecycle_state: SessionLifecycle;
  input_mode: InputMode;
  connected: boolean;
  auto_start: boolean;
  tags: string[];
  recording_enabled: boolean;
  recording_available: boolean;
  owner: string | null;
  visibility: Visibility;
  stopped_at: number | null;
  last_error: string | null;
}

/** The state a session that has never been started is in. */
export const INITIAL_RUNTIME_STATE: SessionRuntimeState = {
  lifecycle_state: "stopped",
  connected: false,
  stopped_at: null,
  last_error: null,
};

/**
 * Whether a session records, resolving the definition's deferral.
 *
 * `null` on the definition means "whatever the deployment decided", which is
 * the recording configuration's `enabled_by_default`. Off unless asked for,
 * both here and there — a port that defaulted it on would record sessions
 * nobody consented to recording.
 */
export function recordingEnabled(definition: SessionDefinition, enabledByDefault: boolean): boolean {
  return definition.recording_enabled ?? enabledByDefault;
}

/**
 * The status object for one session.
 *
 * The key order is the reference's field order. Nothing depends on it — JSON
 * objects are compared as mappings — but a diff of two responses reads much
 * better when it does not have to be sorted first.
 */
export function sessionRuntimeStatus(
  definition: SessionDefinition,
  state: SessionRuntimeState,
  enabledByDefault: boolean,
): SessionRuntimeStatus {
  const recording = recordingEnabled(definition, enabledByDefault);
  return {
    session_id: definition.session_id,
    display_name: definition.display_name,
    created_at: definition.created_at,
    connector_type: definition.connector_type,
    lifecycle_state: state.lifecycle_state,
    input_mode: definition.input_mode,
    connected: state.connected,
    auto_start: definition.auto_start,
    tags: [...definition.tags],
    recording_enabled: recording,
    // The reference reports the same value for both: a session that records
    // has a recording to fetch. They are two fields because they answer two
    // questions, and a client reads whichever it needs.
    recording_available: recording,
    owner: definition.owner,
    visibility: definition.visibility,
    stopped_at: state.stopped_at,
    last_error: state.last_error,
  };
}

/**
 * Fill a configured `[[sessions]]` entry out into a definition.
 *
 * The defaults are `SessionDefinition`'s own, including the one that is not a
 * constant: a session with no display name is named after its id, so nothing
 * a person reads is ever blank.
 *
 * The entry is expected to have been through `serverconfig`'s validation
 * already; what is left here is filling in what was not written.
 */
export function sessionDefinitionFrom(entry: Readonly<Record<string, unknown>>, createdAt: string): SessionDefinition {
  const sessionId = String(entry.session_id ?? "").trim();
  const displayName = entry.display_name === undefined || entry.display_name === null ? "" : String(entry.display_name);
  return {
    session_id: sessionId,
    display_name: displayName || sessionId,
    connector_type: String(entry.connector_type ?? "shell"),
    connector_config: (entry.connector_config as Record<string, unknown>) ?? {},
    input_mode: (entry.input_mode as InputMode) ?? "open",
    auto_start: (entry.auto_start as boolean) ?? true,
    tags: (entry.tags as string[]) ?? [],
    recording_enabled: (entry.recording_enabled as boolean | null) ?? null,
    created_at: entry.created_at === undefined ? createdAt : String(entry.created_at),
    owner: (entry.owner as string | null) ?? null,
    visibility: (entry.visibility as Visibility) ?? "public",
  };
}
