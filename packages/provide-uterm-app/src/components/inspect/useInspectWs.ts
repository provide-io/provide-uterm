//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect, useRef, useCallback } from "react";
import { ControlFrameDecoder } from "../../utils/controlFrames";
import { useInspectStore } from "../../stores/inspectStore";
import {
  parseHttpInterceptStateFrame,
  parseHttpRequestEntry,
  parseHttpResponseEntry,
  ValidationError,
} from "../../api/validators";

export function useInspectWs(sessionId: string) {
  const wsRef = useRef<WebSocket | null>(null);
  const { addRequest, addResponse, syncInterceptState, setWsStatus } = useInspectStore();

  const sendJson = useCallback((msg: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }, []);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws/browser/${encodeURIComponent(sessionId)}/term`;

    const ws = new WebSocket(wsUrl);
    const controlDecoder = new ControlFrameDecoder();
    wsRef.current = ws;

    ws.addEventListener("open", () => setWsStatus("connected"));
    ws.addEventListener("close", () => setWsStatus("disconnected"));

    ws.addEventListener("message", (event) => {
      if (typeof event.data !== "string") return;
      let frames: Array<Record<string, unknown>>;
      try {
        frames = controlDecoder.feed(event.data);
      } catch (err) {
        console.warn(`[inspect-ws] malformed control frame: ${err instanceof Error ? err.message : String(err)}`);
        controlDecoder.reset();
        return;
      }
      for (const frame of frames) {
        if (frame._channel !== "http") continue;
        const type = frame.type as string;
        try {
          if (type === "http_req") {
            addRequest(parseHttpRequestEntry(frame));
          } else if (type === "http_res") {
            addResponse(parseHttpResponseEntry(frame));
          } else if (type === "http_intercept_state") {
            syncInterceptState(parseHttpInterceptStateFrame(frame));
          }
        } catch (err) {
          if (err instanceof ValidationError) {
            console.warn(`[inspect-ws] rejected ${type} frame: ${err.message}`);
          } else {
            throw err;
          }
        }
      }
    });

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [sessionId, addRequest, addResponse, syncInterceptState, setWsStatus]);

  return { sendJson };
}
