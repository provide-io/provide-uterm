//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The nineteen tools that finish the MCP surface.
 *
 * Port of `provide.uterm.ai.server_tools_session` and
 * `provide.uterm.ai.server_tools_gui`: twelve that manage sessions, watch
 * them, broadcast to a group of them and annotate them, and seven that reach
 * a graphical console. Most are the same shape as the hijack tools — validate,
 * call, fold the answer — and what is worth saying is where they are not:
 *
 * * **`session_create` is the widest tool there is.** It can spawn a
 *   connector, so its whole configuration is vetted before any call.
 * * **`session_watch` and `session_subscribe` are clamped.** A model asking
 *   for an hour of events and a million of them gets thirty seconds and
 *   fifty, or two minutes and five hundred.
 * * **`session_subscribe` re-checks the pattern itself** against each event's
 *   screen rather than trusting that events arriving means the pattern fired:
 *   the fallback path, taken when there is no event bus, does not filter.
 * * **Three tools put an id straight into a request path**, which is why it
 *   is checked before anything is sent.
 */

import { compiledPatternOrRejection, rejectBadId, rejectBadPattern } from "./guards.ts";
import { type ClientAnswer, type ToolResult, toolAnswer } from "./hijack-tools.ts";
import { cleanSnapshot, validateSessionCreate } from "./tools.ts";

/** The longest a watch may run, however long was asked for. */
export const WATCH_MAX_SECONDS = 30;

/** The shortest a watch may run, so it cannot be a busy loop. */
export const WATCH_MIN_SECONDS = 0.1;

/** The most events a watch may collect. */
export const WATCH_MAX_EVENTS = 50;

/** The longest a subscription may run. */
export const SUBSCRIBE_MAX_SECONDS = 120;

/** The shortest a subscription may run. */
export const SUBSCRIBE_MIN_SECONDS = 1;

/** The most events a subscription may collect. */
export const SUBSCRIBE_MAX_EVENTS = 500;

/** The session tools, in the order the reference registers them. */
export const SESSION_TOOL_NAMES: readonly string[] = [
  "session_list",
  "session_status",
  "session_read",
  "session_connect",
  "session_disconnect",
  "session_create",
  "session_watch",
  "session_subscribe",
  "fanout_group_create",
  "fanout_send",
  "session_annotate",
];

/** The graphical tools, in the order the reference registers them. */
export const GUI_TOOL_NAMES: readonly string[] = [
  "gui_hijack_begin",
  "gui_hijack_release",
  "gui_screenshot",
  "gui_click",
  "gui_type",
  "gui_key",
  "gui_drag",
];

/** The client these tools drive. */
export interface SessionToolClient {
  listSessions(): Promise<ClientAnswer>;
  getSession(sessionId: string): Promise<ClientAnswer>;
  sessionSnapshot(sessionId: string): Promise<ClientAnswer>;
  connectSession(sessionId: string): Promise<ClientAnswer>;
  disconnectSession(sessionId: string): Promise<ClientAnswer>;
  quickConnect(connectorType: string, options: Record<string, unknown>): Promise<ClientAnswer>;
  watchSessionEvents(
    sessionId: string,
    options: { event_types: string | null; pattern: string | null; timeout_ms: number; max_events: number },
  ): Promise<ClientAnswer>;
  post(path: string, body: unknown): Promise<ClientAnswer>;
  acquire(workerId: string, options: { owner: string; lease_s: number }): Promise<ClientAnswer>;
  release(workerId: string, hijackId: string): Promise<ClientAnswer>;
  guiScreenshot(workerId: string, hijackId: string): Promise<ClientAnswer>;
  guiClick(
    workerId: string,
    hijackId: string,
    options: { x: number; y: number; button: string },
  ): Promise<ClientAnswer>;
  guiType(workerId: string, hijackId: string, options: { text: string }): Promise<ClientAnswer>;
  guiKey(workerId: string, hijackId: string, options: { key_name: string }): Promise<ClientAnswer>;
  guiDrag(
    workerId: string,
    hijackId: string,
    options: { start_x: number; start_y: number; end_x: number; end_y: number },
  ): Promise<ClientAnswer>;
}

/** A field as text, or the default. */
function text(args: Record<string, unknown>, key: string, fallback = ""): string {
  const raw = args[key];
  return typeof raw === "string" ? raw : fallback;
}

/** A field as a number, or the default. */
function count(args: Record<string, unknown>, key: string, fallback: number): number {
  const raw = args[key];
  return typeof raw === "number" ? raw : fallback;
}

/** A field that may simply be absent. */
function optional(args: Record<string, unknown>, key: string): string | null {
  const raw = args[key];
  return typeof raw === "string" ? raw : null;
}

/** Hold a value between two ends, whichever way it was out. */
function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/**
 * Whether a snapshot is a mapping with something in it.
 *
 * The same reading `hijack_read` uses, and for the same reason: an empty
 * mapping is false in Python and true here, and only a mapping can be
 * cleaned.
 */
function isCleanableSnapshot(value: unknown): boolean {
  return typeof value === "object" && value !== null && Object.keys(value).length > 0;
}

/**
 * Call one of the tools by name.
 *
 * @throws {Error} For a name nobody registered.
 */
export async function callSessionTool(
  client: SessionToolClient,
  tool: string,
  args: Record<string, unknown>,
): Promise<ToolResult> {
  switch (tool) {
    case "session_list":
      return foldOf(await client.listSessions());
    case "session_status":
      return withSession(args, (sessionId) => client.getSession(sessionId));
    case "session_read":
      return sessionRead(client, args);
    case "session_connect":
      return withSession(args, (sessionId) => client.connectSession(sessionId));
    case "session_disconnect":
      return withSession(args, (sessionId) => client.disconnectSession(sessionId));
    case "session_create":
      return sessionCreate(client, args);
    case "session_watch":
      return sessionWatch(client, args);
    case "session_subscribe":
      return sessionSubscribe(client, args);
    case "fanout_group_create":
      return fanoutGroupCreate(client, args);
    case "fanout_send":
      return fanoutSend(client, args);
    case "session_annotate":
      return sessionAnnotate(client, args);
    case "gui_hijack_begin":
      return guiHijackBegin(client, args);
    case "gui_hijack_release":
      return withWorkerAndHijack(args, (workerId, hijackId) => client.release(workerId, hijackId));
    case "gui_screenshot":
      return withWorkerAndHijack(args, (workerId, hijackId) => client.guiScreenshot(workerId, hijackId));
    case "gui_click":
      return withWorkerAndHijack(args, (workerId, hijackId) =>
        client.guiClick(workerId, hijackId, {
          x: count(args, "x", 0),
          y: count(args, "y", 0),
          button: text(args, "button", "left"),
        }),
      );
    case "gui_type":
      return withWorkerAndHijack(args, (workerId, hijackId) =>
        client.guiType(workerId, hijackId, { text: text(args, "text") }),
      );
    case "gui_key":
      return withWorkerAndHijack(args, (workerId, hijackId) =>
        client.guiKey(workerId, hijackId, { key_name: text(args, "key_name") }),
      );
    case "gui_drag":
      return withWorkerAndHijack(args, (workerId, hijackId) =>
        client.guiDrag(workerId, hijackId, {
          start_x: count(args, "start_x", 0),
          start_y: count(args, "start_y", 0),
          end_x: count(args, "end_x", 0),
          end_y: count(args, "end_y", 0),
        }),
      );
    default:
      throw new Error(`unknown tool: ${tool}`);
  }
}

/** Fold a client answer, which is what every one of these ends with. */
function foldOf(answer: ClientAnswer): ToolResult {
  return toolAnswer(answer.ok, answer.data);
}

/** Check the session id, then do the thing. */
async function withSession(
  args: Record<string, unknown>,
  call: (sessionId: string) => Promise<ClientAnswer>,
): Promise<ToolResult> {
  const sessionId = text(args, "session_id");
  const rejection = rejectBadId(sessionId, "session_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  return foldOf(await call(sessionId));
}

/**
 * Check both ids, then do the thing.
 *
 * The graphical tools all take a worker and a hijack, and all check them the
 * same way — the reference reports whichever is bad first.
 */
async function withWorkerAndHijack(
  args: Record<string, unknown>,
  call: (workerId: string, hijackId: string) => Promise<ClientAnswer>,
): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const hijackId = text(args, "hijack_id");
  const rejection = rejectBadId(workerId, "worker_id") ?? rejectBadId(hijackId, "hijack_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  return foldOf(await call(workerId, hijackId));
}

/** Read a session's screen. */
export async function sessionRead(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const sessionId = text(args, "session_id");
  const rejection = rejectBadId(sessionId, "session_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const answer = await client.sessionSnapshot(sessionId);
  const result = toolAnswer(answer.ok, answer.data);
  if (answer.ok && isCleanableSnapshot(result.snapshot)) {
    const tailLines = typeof args.tail_lines === "number" ? args.tail_lines : undefined;
    result.snapshot = cleanSnapshot(
      result.snapshot as Record<string, unknown>,
      text(args, "output", "text"),
      tailLines,
    );
  }
  return result;
}

/** Start a session, once its configuration has been vetted. */
export async function sessionCreate(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const connectorType = text(args, "connector_type");
  // Before any call: this tool can spawn a connector, so a refusal after the
  // fact would be a refusal after the damage.
  const rejection = validateSessionCreate({
    connectorType,
    url: optional(args, "url") ?? undefined,
    port: typeof args.port === "number" ? args.port : undefined,
    host: optional(args, "host") ?? undefined,
  });
  if (rejection !== undefined) {
    return { ...rejection };
  }

  // Only what was actually given: sending `username: null` would be a request
  // to connect as nobody.
  const options: Record<string, unknown> = {};
  for (const key of ["display_name", "host", "port", "url", "username", "password", "input_mode"]) {
    if (args[key] !== undefined && args[key] !== null) {
      options[key] = args[key];
    }
  }
  return foldOf(await client.quickConnect(connectorType, options));
}

/** Watch a session for a short while. */
export async function sessionWatch(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const sessionId = text(args, "session_id");
  const badId = rejectBadId(sessionId, "session_id");
  if (badId !== undefined) {
    return { ...badId };
  }
  const pattern = optional(args, "pattern");
  const badPattern = rejectBadPattern(pattern);
  if (badPattern !== undefined) {
    return { ...badPattern };
  }
  return foldOf(
    await client.watchSessionEvents(sessionId, {
      event_types: optional(args, "event_types"),
      pattern,
      // Clamped both ways: an hour is not on offer, and neither is a call
      // that returns instantly and can be repeated forever.
      timeout_ms: Math.trunc(clamp(count(args, "timeout_s", 10), WATCH_MIN_SECONDS, WATCH_MAX_SECONDS) * 1000),
      max_events: clamp(count(args, "max_events", WATCH_MAX_EVENTS), 1, WATCH_MAX_EVENTS),
    }),
  );
}

/** Subscribe to a session for as long as an agent loop needs. */
export async function sessionSubscribe(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const sessionId = text(args, "session_id");
  const badId = rejectBadId(sessionId, "session_id");
  if (badId !== undefined) {
    return { ...badId };
  }
  const pattern = optional(args, "pattern");
  // Compiled once here and reused below, rather than validated and then
  // compiled again.
  const [compiled, badPattern] = compiledPatternOrRejection(pattern);
  if (badPattern !== undefined) {
    return { ...badPattern };
  }

  const answer = await client.watchSessionEvents(sessionId, {
    event_types: optional(args, "event_types"),
    pattern,
    timeout_ms: Math.trunc(clamp(count(args, "duration_s", 30), SUBSCRIBE_MIN_SECONDS, SUBSCRIBE_MAX_SECONDS) * 1000),
    max_events: clamp(count(args, "max_events", 200), 1, SUBSCRIBE_MAX_EVENTS),
  });

  // Checked here rather than inferred: the fallback path, taken when there is
  // no event bus, does not filter, so events arriving proves nothing about
  // whether the pattern fired.
  const result = toolAnswer(answer.ok, answer.data);
  result.matched_pattern = compiled !== undefined && answer.ok && patternFired(compiled, result.events);
  return result;
}

/** Whether a pattern matched any event's screen. */
function patternFired(pattern: RegExp, events: unknown): boolean {
  if (!Array.isArray(events)) {
    return false;
  }
  for (const event of events) {
    // Everything here came off the wire, so an entry that is not shaped like
    // an event must be stepped over rather than allowed to stop the scan.
    if (typeof event !== "object" || event === null) {
      continue;
    }
    const payload = (event as Record<string, unknown>).data;
    const screen = typeof payload === "object" && payload !== null ? (payload as Record<string, unknown>).screen : "";
    // Rendered as text, as the reference renders it: a number on the screen
    // is matched as the digits it prints as.
    if (pattern.test(typeof screen === "string" ? screen : String(screen ?? ""))) {
      return true;
    }
  }
  return false;
}

/** Make a group so one command can reach many sessions. */
export async function fanoutGroupCreate(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const sessionIds = Array.isArray(args.session_ids) ? args.session_ids : [];
  return foldOf(
    await client.post("/api/fanout/groups", {
      name: text(args, "name", "fleet"),
      worker_ids: sessionIds,
      mode: text(args, "mode", "parallel"),
    }),
  );
}

/** Broadcast to every session in a group. */
export async function fanoutSend(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const groupId = text(args, "group_id");
  // The id lands in the request path, so a slash is a different route.
  const rejection = rejectBadId(groupId, "group_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  return foldOf(
    await client.post(`/api/fanout/groups/${groupId}/send`, {
      data: text(args, "data"),
      quiesce_ms: count(args, "quiesce_ms", 500),
      max_response_ms: count(args, "max_response_ms", 10000),
    }),
  );
}

/** Mark a moment on a session's recording. */
export async function sessionAnnotate(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const sessionId = text(args, "session_id");
  const rejection = rejectBadId(sessionId, "session_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  return foldOf(
    await client.post(`/api/sessions/${sessionId}/annotate`, {
      label: text(args, "label"),
      description: text(args, "description"),
      severity: text(args, "severity", "info"),
    }),
  );
}

/** Take a lease for graphical work, which is the same lease as any other. */
export async function guiHijackBegin(client: SessionToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const rejection = rejectBadId(workerId, "worker_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  return foldOf(
    await client.acquire(workerId, { owner: text(args, "owner", "operator"), lease_s: count(args, "lease_s", 90) }),
  );
}
