//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A live session, as React state.
 *
 * The socket is injected rather than opened here, so a view can be driven
 * without a network and the same hook serves a browser, a test and a
 * server-rendered page.
 */

import { useCallback, useEffect, useReducer, useRef } from "react";
import { ControlFrameDecoder, encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import { canType, INITIAL_STATE, type SessionEvent, type SessionState, sessionReducer } from "./session-state.ts";

/** The socket a session runs over — as much of one as this needs. */
export interface SessionSocket {
  send(message: string): void;
  close(): void;
  /** Register the handlers. Returns a function that removes them again. */
  listen(handlers: { onOpen(): void; onMessage(message: string): void; onClose(): void }): () => void;
}

/** What the hook needs to know. */
export interface UseSessionOptions {
  /** Opens a socket. Called once per mount. */
  connect: () => SessionSocket;
  /** This viewer's own identifier, which decides whether they hold the lease. */
  viewerId?: string | undefined;
}

/** A live session and the things a view can do to it. */
export interface Session {
  state: SessionState;
  /** Whether this viewer may type right now. */
  canType: boolean;
  /** Send a keystroke. Ignored when this viewer may not type. */
  sendInput(data: string): void;
  /** Ask for the right to type. */
  requestHijack(): void;
  /** Give it back. */
  releaseHijack(): void;
  /** Answer a pending request. */
  resolveApproval(id: string, allowed: boolean): void;
  /** Forget what has been shown, locally. */
  clear(): void;
}

/** Drive a session from a socket. */
export function useSession(options: UseSessionOptions): Session {
  const { connect, viewerId } = options;
  const socketRef = useRef<SessionSocket | undefined>(undefined);
  // Held in a ref rather than named as a dependency. A caller writing
  // `connect: () => socket` inline — the obvious way to write it — hands over a
  // new function on every render, and an effect keyed on that would close and
  // reopen the connection each time anything at all changed.
  const connectRef = useRef(connect);
  connectRef.current = connect;

  const [state, dispatch] = useReducer(
    (current: SessionState, event: SessionEvent) => sessionReducer(current, event, viewerId),
    INITIAL_STATE,
  );

  useEffect(() => {
    const socket = connectRef.current();
    socketRef.current = socket;
    // One decoder for the whole connection: a frame can straddle two
    // messages, and decoding each message alone would split it in half.
    const decoder = new ControlFrameDecoder();
    const stop = socket.listen({
      onOpen: () => dispatch({ kind: "opened" }),
      onMessage: (message) => {
        for (const chunk of decoder.feed(message)) {
          dispatch(
            chunk.kind === "data" ? { kind: "data", data: chunk.data } : { kind: "control", frame: chunk.control },
          );
        }
      },
      onClose: () => dispatch({ kind: "closed" }),
    });
    return () => {
      stop();
      socket.close();
      socketRef.current = undefined;
    };
    // Once per mount: a session is a connection, and re-running this is
    // hanging up on somebody mid-sentence.
  }, []);

  const send = useCallback((message: string) => {
    socketRef.current?.send(message);
  }, []);

  const allowed = canType(state);

  const sendInput = useCallback(
    (data: string) => {
      // Refused here as well as by the server: a viewer whose keystrokes are
      // going to be dropped should not see them travel.
      if (allowed && data !== "") {
        send(encodeTerminalData(data));
      }
    },
    [allowed, send],
  );

  const requestHijack = useCallback(() => {
    send(encodeControlFrame({ type: "hijack_request" }));
  }, [send]);

  const releaseHijack = useCallback(() => {
    send(encodeControlFrame({ type: "hijack_release" }));
  }, [send]);

  const resolveApproval = useCallback(
    (id: string, approved: boolean) => {
      send(encodeControlFrame({ type: "approval_resolved", approval_id: id, approved }));
    },
    [send],
  );

  const clear = useCallback(() => {
    dispatch({ kind: "cleared" });
  }, []);

  return { state, canType: allowed, sendInput, requestHijack, releaseHijack, resolveApproval, clear };
}
