//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a viewer knows about a session, and how each frame changes it.
 *
 * The frames themselves are the wire format already ported in `frames`; this
 * is the state a user interface renders from them, kept apart from any
 * rendering so it can be reasoned about on its own.
 *
 * The rule that matters most: **whether this viewer may type is decided by
 * the server, never here.** The interface asks; the hub answers with a
 * `hijack_state` naming the holder. A client that decided for itself would
 * let two people type into one shell the moment a message was lost.
 */

/** How the connection itself is doing. */
export type ConnectionStatus = "connecting" | "open" | "closed";

/** Somebody else looking at the same session. */
export interface Participant {
  id: string;
  name: string;
  role: string;
  colour: string | undefined;
}

/** A request waiting for somebody to allow or refuse it. */
export interface PendingApproval {
  id: string;
  subject: string;
  reason: string | undefined;
}

/** Everything a session view renders from. */
export interface SessionState {
  status: ConnectionStatus;
  /** What the terminal has shown, oldest first. */
  screen: string;
  /** The session's own identifier, once the server has named it. */
  sessionId: string | undefined;
  /** Who currently holds the right to type, if anybody. */
  hijackHolder: string | undefined;
  /** Whether *this* viewer is the holder. */
  isHolder: boolean;
  /** The input mode the worker last announced. */
  inputMode: string;
  /** Everybody present, in the order the server listed them. */
  participants: Participant[];
  /** Requests awaiting a decision, oldest first. */
  approvals: PendingApproval[];
  /** The last error the server reported, if any. */
  error: string | undefined;
  /** How many times the connection has been re-established. */
  reconnects: number;
}

/** The state a view starts in, before anything has arrived. */
export const INITIAL_STATE: SessionState = {
  status: "connecting",
  screen: "",
  sessionId: undefined,
  hijackHolder: undefined,
  isHolder: false,
  inputMode: "open",
  participants: [],
  approvals: [],
  error: undefined,
  reconnects: 0,
};

/** How much of the session a view keeps, matching the connector's own cap. */
export const SCREEN_CAP = 32768;

/** Something that happened to the session. */
export type SessionEvent =
  | { kind: "opened" }
  | { kind: "closed" }
  | { kind: "data"; data: string }
  | { kind: "control"; frame: Record<string, unknown> }
  | { kind: "cleared" };

/** Read a string field, or nothing when it is absent or the wrong type. */
function text(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

/** Read the participants out of a presence frame. */
function readParticipants(value: unknown): Participant[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((entry) => {
    if (typeof entry !== "object" || entry === null) {
      return [];
    }
    const record = entry as Record<string, unknown>;
    const id = text(record.id) ?? text(record.viewer_id);
    if (id === undefined) {
      // Somebody with no identifier cannot be told from anybody else, and
      // showing them would put an anonymous row in the list for every frame.
      return [];
    }
    return [
      {
        id,
        name: text(record.name) ?? text(record.display_name) ?? id,
        role: text(record.role) ?? "viewer",
        colour: text(record.color) ?? text(record.colour),
      },
    ];
  });
}

/** Append to the screen, keeping only what a view can show. */
function appendScreen(screen: string, data: string): string {
  const combined = screen + data;
  // From the end, as the connector caps its own buffer: the newest output is
  // what somebody is looking at.
  return combined.length > SCREEN_CAP ? combined.slice(-SCREEN_CAP) : combined;
}

/** Apply a control frame. */
function applyControl(state: SessionState, frame: Record<string, unknown>, viewerId: string | undefined): SessionState {
  switch (frame.type) {
    case "hello":
      return { ...state, sessionId: text(frame.session_id) ?? state.sessionId };

    case "hijack_state": {
      const holder = text(frame.holder) ?? text(frame.holder_id);
      return {
        ...state,
        hijackHolder: holder,
        // Decided by comparing the server's answer with this viewer's own
        // identity — never by what this client last asked for.
        //
        // The `viewerId` check cannot change the answer on its own: an
        // anonymous viewer fails the comparison anyway. It is here to say that
        // an unnamed viewer holds nothing, rather than leaving that to a
        // coincidence of two undefined values not being compared.
        isHolder: holder !== undefined && viewerId !== undefined && holder === viewerId,
      };
    }

    case "input_mode_changed":
    case "worker_hello":
      return { ...state, inputMode: text(frame.input_mode) ?? state.inputMode };

    case "presence_sync":
      return { ...state, participants: readParticipants(frame.participants ?? frame.viewers) };

    case "presence_update": {
      const incoming = readParticipants([frame.participant ?? frame]);
      if (incoming.length === 0) {
        return state;
      }
      const participant = incoming[0] as Participant;
      const existing = state.participants.findIndex((other) => other.id === participant.id);
      if (existing === -1) {
        return { ...state, participants: [...state.participants, participant] };
      }
      // Replaced in place, so somebody who changes role does not jump to the
      // end of a list people are reading.
      const participants = [...state.participants];
      participants[existing] = participant;
      return { ...state, participants };
    }

    case "presence_leave": {
      const id = text(frame.viewer_id) ?? text(frame.id);
      return id === undefined
        ? state
        : { ...state, participants: state.participants.filter((other) => other.id !== id) };
    }

    case "approval_pending": {
      const id = text(frame.approval_id) ?? text(frame.id);
      if (id === undefined || state.approvals.some((approval) => approval.id === id)) {
        // A repeated prompt is the server retrying, not a second request.
        return state;
      }
      return {
        ...state,
        approvals: [...state.approvals, { id, subject: text(frame.subject) ?? "somebody", reason: text(frame.reason) }],
      };
    }

    case "approval_resolved": {
      const id = text(frame.approval_id) ?? text(frame.id);
      return id === undefined
        ? state
        : { ...state, approvals: state.approvals.filter((approval) => approval.id !== id) };
    }

    case "error":
      return { ...state, error: text(frame.message) ?? text(frame.error) ?? "unknown error" };

    default:
      // A frame this version does not know is not a reason to lose the
      // session: a newer server may say more than an older viewer understands.
      return state;
  }
}

/**
 * The session's state after one more thing has happened.
 *
 * Pure, so a view's behaviour can be checked without a view.
 */
export function sessionReducer(
  state: SessionState,
  event: SessionEvent,
  viewerId: string | undefined = undefined,
): SessionState {
  switch (event.kind) {
    case "opened":
      // A reconnection keeps the screen: the session is the same one, and
      // clearing it would lose what somebody was reading.
      return {
        ...state,
        status: "open",
        error: undefined,
        reconnects: state.status === "closed" ? state.reconnects + 1 : state.reconnects,
      };

    case "closed":
      // Everything that depends on the server is dropped, because none of it
      // is being kept up to date any more. A stale presence list is a list of
      // people who may have left.
      return {
        ...state,
        status: "closed",
        hijackHolder: undefined,
        isHolder: false,
        participants: [],
        approvals: [],
      };

    case "data":
      return { ...state, screen: appendScreen(state.screen, event.data) };

    case "cleared":
      return { ...state, screen: "" };

    case "control":
      return applyControl(state, event.frame, viewerId);
  }
}

/**
 * Whether this viewer may type right now.
 *
 * Three things have to hold: the connection is up, the worker is not locked,
 * and — when somebody holds the session — that somebody is this viewer.
 */
export function canType(state: SessionState): boolean {
  if (state.status !== "open") {
    return false;
  }
  if (state.inputMode === "hijack") {
    return state.isHolder;
  }
  return state.hijackHolder === undefined || state.isHolder;
}
