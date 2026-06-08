//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { DeckMux } from "@provide-uterm-frontend/app/deckmux/deckmux";
// Side-effect import: registers the <uterm-session> custom element. The React
// app renders the tag directly, so it must ensure the element is defined rather
// than relying on the vanilla hijack.ts entry being loaded on the page.
import "@provide-uterm-frontend/session-element";
import type { UtermSessionElement } from "@provide-uterm-frontend/session-element";
import type { DetailedHTMLProps, HTMLAttributes, Ref } from "react";
import { useEffect, useRef } from "react";
import type { SessionSurface } from "../../api/types";
import { useTerminalStore } from "../../stores/terminalStore";

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "uterm-session": DetailedHTMLProps<HTMLAttributes<HTMLElement>, HTMLElement> & {
        config?: unknown;
        ref?: Ref<UtermSessionElement>;
      };
    }
  }
}

interface HijackHostProps {
  sessionId: string;
  surface?: SessionSurface;
}

export function HijackHost({ sessionId, surface }: HijackHostProps) {
  const containerRef = useRef<UtermSessionElement>(null);
  const mountedRef = useRef(false);
  const setMounted = useTerminalStore((s) => s.setMounted);
  const setDimensions = useTerminalStore((s) => s.setDimensions);

  useEffect(() => {
    if (mountedRef.current || !containerRef.current) return;
    const isOperator = surface === "operator";
    mountedRef.current = true;
    setMounted(true);

    // DeckMux state — lazily initialised on first presence_sync
    let deckMux: DeckMux | null = null;
    const presenceUsers = new Map<string, { name: string; color: string }>();
    const widget = containerRef.current;
    const ownDimsRef = { cols: 0, rows: 0 };
    let myUserId: string | null = null;

    widget.config = {
      workerId: sessionId,
      showAnalysis: isOperator,
      mobileKeys: isOperator,
      onResize: (cols: number, rows: number) => {
        setDimensions(cols, rows);
        ownDimsRef.cols = cols;
        ownDimsRef.rows = rows;
        deckMux?.setOwnDimensions(cols, rows);
        widget.sendControlMessage({ type: "presence_update", cols, rows });
        // Update own avatar directly — don't wait for server echo which can race
        if (deckMux && myUserId) {
          deckMux.handleMessage({ type: "dm_presence", user_id: myUserId, cols, rows });
        }
      },
      onPresenceMessage(msg: Record<string, unknown>) {
        const type = msg.type as string;

        if (type === "presence_sync") {
          if (!deckMux) {
            const termDiv = widget.terminalElement;
            if (!termDiv) return;
            const rawCfg = (msg.config as Record<string, unknown> | undefined) ?? {};
            deckMux = new DeckMux(termDiv, null);
            deckMux.enable({
              autoTransferIdleS: (rawCfg.auto_transfer_idle_s as number | undefined) ?? 30,
              keystrokeQueue: (rawCfg.keystroke_queue as string | undefined) === "replay" ? "replay" : "display",
              ghostBox: rawCfg.ghost_box !== false,
            });
            if (ownDimsRef.cols > 0) deckMux.setOwnDimensions(ownDimsRef.cols, ownDimsRef.rows);
            // Trigger FitAddon resize after the presence bar is inserted into the layout
            requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
          }
          const users = (msg.users as Array<Record<string, unknown>> | undefined) ?? [];
          const myUser = users[users.length - 1];
          if (myUser && !myUserId) {
            myUserId = myUser.user_id as string;
            deckMux.handleMessage({ type: "dm_hello", user_id: myUser.user_id });
          }
          for (const u of users) {
            const uid = u.user_id as string | undefined;
            if (uid) presenceUsers.set(uid, { name: String(u.name ?? uid), color: String(u.color ?? "#888") });
            deckMux.handleMessage({ ...u, type: "dm_join" });
          }
          if (typeof msg.owner_id === "string") {
            deckMux.handleMessage({ type: "dm_owner_change", user_id: msg.owner_id });
          }
          widget.sendControlMessage({ type: "presence_update", scroll_line: 0, scroll_range: [0, 1] });
          // Push known dims into own avatar immediately (no server roundtrip needed)
          if (myUserId && ownDimsRef.cols > 0) {
            deckMux.handleMessage({
              type: "dm_presence",
              user_id: myUserId,
              cols: ownDimsRef.cols,
              rows: ownDimsRef.rows,
            });
          }
        } else if (type === "presence_update") {
          const uid = msg.user_id as string | undefined;
          if (uid) {
            const c = presenceUsers.get(uid);
            if (c) {
              if (typeof msg.name === "string") c.name = msg.name;
              if (typeof msg.color === "string") c.color = msg.color;
            }
          }
          deckMux?.handleMessage({ ...msg, type: "dm_presence" });
        } else if (type === "presence_leave") {
          deckMux?.handleMessage({ ...msg, type: "dm_leave" });
        } else if (type === "control_transfer") {
          const toId = msg.to_user_id as string | undefined;
          const fromId = msg.from_user_id as string | undefined;
          deckMux?.handleMessage({ type: "dm_owner_change", user_id: toId ?? null });
          if (toId) {
            const u = presenceUsers.get(toId);
            deckMux?.handleMessage({
              type: "dm_control_transfer",
              to_user_id: toId,
              to_name: u?.name ?? toId,
              to_color: u?.color ?? "#888",
              from_user_id: fromId ?? "",
            });
          }
        }
      },
    };
    
    // Explicitly connect the session
    widget.connect();

    // Forward xterm scroll events → presence_update (normalized to 0-1 fractions).
    // Named (not inline) so the cleanup can removeEventListener it — see return below.
    const onScroll = (e: Event) => {
      const { viewportY, rows, totalLines } = (
        e as CustomEvent<{ viewportY: number; rows: number; totalLines?: number }>
      ).detail;
      const total = Math.max(rows, totalLines ?? rows);
      const normTop = viewportY / total;
      const normBottom = (viewportY + rows) / total;
      widget.sendControlMessage({
        type: "presence_update",
        scroll_line: normTop,
        scroll_range: [normTop, normBottom],
      });
    };
    widget.addEventListener("uterm:scroll", onScroll);

    // Forward DeckMux outbound events → WS (request_control and hand_off both ask for control)
    const onRequestControl = () => {
      widget.sendControlMessage({ type: "control_request" });
    };
    widget.addEventListener("deckmux:request_control", onRequestControl);
    widget.addEventListener("deckmux:hand_off", onRequestControl);

    // Keepalive: send dims every 15 s so server-side idle pruning can evict dead connections
    const keepaliveInterval = setInterval(() => {
      if (ownDimsRef.cols > 0) {
        widget.sendControlMessage({ type: "presence_update", cols: ownDimsRef.cols, rows: ownDimsRef.rows });
      }
    }, 15_000);

    return () => {
      widget.removeEventListener("uterm:scroll", onScroll);
      widget.removeEventListener("deckmux:request_control", onRequestControl);
      widget.removeEventListener("deckmux:hand_off", onRequestControl);
      clearInterval(keepaliveInterval);
      mountedRef.current = false;
    };
  }, [sessionId, surface, setMounted, setDimensions]);

  return <uterm-session ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}

