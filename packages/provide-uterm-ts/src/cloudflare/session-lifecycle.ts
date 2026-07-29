//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a session runtime does when a socket opens, closes or fails.
 *
 * Port of `provide.uterm.cloudflare.do.session_runtime.lifecycle`. The
 * plumbing is a Durable Object's; the decisions are not, and they are what is
 * here — as a list of actions a host then carries out, so the same rules can
 * be read, tested and reasoned about without a Cloudflare runtime.
 *
 * Three of those decisions matter more than the rest.
 *
 * **What a browser is told on connect.** The hello frame carries
 * `can_hijack`, and the *resolved* role decides it — the one a JWT produced,
 * not the one the browser asked for. A port that read the requested role would
 * put hijack controls in front of a viewer.
 *
 * **Whether hello is sent at all.** On a normal upgrade the `fetch` handler
 * already sent it before the 101 response; on a hibernation restore that
 * handler never ran for this connection. The runtime tells the two apart by
 * whether the socket is already known, and getting it wrong means a browser
 * receives two hellos or none.
 *
 * **What a disconnect leaves behind.** A browser that held the hijack has its
 * resume token marked so it can reclaim ownership on reconnect; a worker
 * leaving moves the session to `stopped` on a clean close and `error` on a
 * failure. A session already deleted does none of it — there is nobody left to
 * tell, and the socket is closed instead.
 *
 * A socket's role is read from its attachment rather than from object
 * identity, because after hibernation the runtime's own references are gone
 * and an identity check would always be false.
 */

import {
  CURRENT_PROTOCOL_VERSION,
  MAX_PROTOCOL_VERSION,
  MIN_PROTOCOL_VERSION,
  PREFERRED_PROTOCOL_VERSION,
} from "../bridge/index.ts";

/** What a socket is here for. */
export type SocketRole = "worker" | "browser" | "raw";

/** One thing the runtime should do, for its host to carry out. */
export type LifecycleAction =
  | { kind: "close"; code: number; reason: string }
  | { kind: "register_socket"; role: SocketRole }
  | { kind: "remove_socket" }
  | { kind: "broadcast_worker_frame"; frame: Record<string, unknown> }
  | { kind: "broadcast_browsers"; frame: Record<string, unknown> }
  | { kind: "send"; frame: Record<string, unknown> }
  | { kind: "send_text"; text: string }
  | { kind: "presence_sync"; exclude_self: boolean }
  | { kind: "send_hijack_state" }
  | { kind: "create_resume_token"; token: string; worker_id: string; role: string; ttl: number }
  | { kind: "mark_resume_hijack_owner"; token: string; owner: boolean }
  | { kind: "update_kv"; connected: boolean }
  | { kind: "on_browser_connected" };

/** How long a resume token lives when nobody says otherwise. */
export const DEFAULT_RESUME_TTL_S = 300;

/** What the runtime knows when a socket opens. */
export interface SocketOpenState {
  role: SocketRole;
  /** A deleted session tells nobody anything. */
  deleted: boolean;
  workerId: string;
  /** The socket's own identifier, used as a presence id. */
  wsId: string;
  /** The role a JWT resolved, which is what `can_hijack` follows. */
  browserRole: string;
  inputMode: string;
  presence: boolean;
  /** Whether a worker or a ushell is behind this session. */
  workerOnline: boolean;
  /** False on a hibernation restore, where `fetch` never ran. */
  alreadyInitialized: boolean;
  resumeEnabled: boolean;
  resumeTtlS?: number;
  /** The token to hand out, which the host generates. */
  resumeToken: string;
  /** The screen to replay, when there is one. */
  lastSnapshot?: Record<string, unknown> | undefined;
  now: number;
}

/** What the runtime knows when a socket goes. */
export interface SocketCloseState {
  role: SocketRole;
  deleted: boolean;
  workerId: string;
  wsId: string;
  presence: boolean;
  /** Whether this browser was the one holding the hijack. */
  heldHijack: boolean;
  /** The resume token this browser was given, if any. */
  resumeToken?: string | undefined;
  now: number;
}

/**
 * The hello frame a browser is sent.
 *
 * `can_hijack` follows the resolved role and nothing else: only an admin may
 * take a session over, so anything that is not exactly `admin` — including a
 * role nobody defined — gets false.
 */
export function buildHelloFrame(state: SocketOpenState): Record<string, unknown> {
  const hello: Record<string, unknown> = {
    type: "hello",
    worker_id: state.workerId,
    worker_online: state.workerOnline,
    can_hijack: state.browserRole === "admin",
    input_mode: state.inputMode,
    role: state.browserRole,
    hijack_control: "rest",
    hijack_step_supported: true,
    resume_supported: state.resumeEnabled,
    presence_enabled: state.presence,
    protocol_version: CURRENT_PROTOCOL_VERSION,
    protocol: {
      selected: PREFERRED_PROTOCOL_VERSION,
      server_min: MIN_PROTOCOL_VERSION,
      server_max: MAX_PROTOCOL_VERSION,
    },
    ts: state.now,
  };
  if (state.resumeEnabled) {
    hello.resume_token = state.resumeToken;
  }
  return hello;
}

/**
 * What an opening socket causes.
 *
 * A deleted session closes it with 1001 and does nothing else.
 */
export function onSocketOpen(state: SocketOpenState): LifecycleAction[] {
  if (state.deleted) {
    return [{ kind: "close", code: 1001, reason: "session deleted" }];
  }
  const actions: LifecycleAction[] = [{ kind: "register_socket", role: state.role }];

  if (state.role === "worker") {
    actions.push(
      { kind: "broadcast_worker_frame", frame: { type: "worker_connected", worker_id: state.workerId, ts: state.now } },
      { kind: "update_kv", connected: true },
    );
    return actions;
  }

  if (state.role === "raw") {
    // A raw socket gets the screen as text and nothing else — it is a plain
    // terminal, not a client that understands frames.
    const screen = state.lastSnapshot?.screen;
    if (typeof screen === "string") {
      actions.push({ kind: "send_text", text: screen });
    }
    return actions;
  }

  if (!state.alreadyInitialized) {
    // The hibernation-restore path: `fetch` never ran for this connection, so
    // hello has to be sent here. On a normal upgrade it already went.
    if (state.resumeEnabled) {
      actions.push({
        kind: "create_resume_token",
        token: state.resumeToken,
        worker_id: state.workerId,
        role: state.browserRole,
        ttl: state.resumeTtlS ?? DEFAULT_RESUME_TTL_S,
      });
    }
    actions.push({ kind: "send", frame: buildHelloFrame(state) });
  }
  actions.push({ kind: "presence_sync", exclude_self: true }, { kind: "send_hijack_state" });
  if (state.lastSnapshot !== undefined) {
    actions.push({ kind: "send", frame: state.lastSnapshot });
  }
  actions.push({ kind: "on_browser_connected" });
  return actions;
}

/**
 * What a departing socket causes.
 *
 * A close and a failure do exactly the same things. They differ only in the
 * lifecycle state a departing worker leaves behind, and that is
 * {@link lifecycleStateAfter}'s answer rather than an action — so there is one
 * body here and not two.
 */
function onSocketGone(state: SocketCloseState): LifecycleAction[] {
  const actions: LifecycleAction[] = [];
  if (state.role === "browser" && !state.deleted) {
    if (state.presence) {
      actions.push({
        kind: "broadcast_browsers",
        frame: { type: "presence_leave", user_id: state.wsId, ts: state.now },
      });
    }
    // Marked before the socket goes, so a browser that was driving can
    // reclaim ownership when it comes back.
    if (state.heldHijack && state.resumeToken !== undefined && state.resumeToken !== "") {
      actions.push({ kind: "mark_resume_hijack_owner", token: state.resumeToken, owner: true });
    }
  }
  actions.push({ kind: "remove_socket" });
  if (state.role === "worker") {
    if (!state.deleted) {
      actions.push({
        kind: "broadcast_worker_frame",
        frame: { type: "worker_disconnected", worker_id: state.workerId, ts: state.now },
      });
    }
    actions.push({ kind: "update_kv", connected: false });
  }
  return actions;
}

/** What a clean close causes. A departing worker leaves the session stopped. */
export function onSocketClose(state: SocketCloseState): LifecycleAction[] {
  return onSocketGone(state);
}

/** What a failure causes. A departing worker leaves the session in error. */
export function onSocketError(state: SocketCloseState): LifecycleAction[] {
  return onSocketGone(state);
}

/** The lifecycle state a socket event leaves the session in, or nothing. */
export function lifecycleStateAfter(
  event: "open" | "close" | "error",
  state: { role: SocketRole; deleted: boolean },
): "running" | "stopped" | "error" | undefined {
  if (state.role !== "worker") {
    return undefined;
  }
  if (event === "open") {
    return state.deleted ? undefined : "running";
  }
  // A deleted session's state is not moved: there is nothing left to run.
  if (state.deleted) {
    return undefined;
  }
  return event === "close" ? "stopped" : "error";
}
