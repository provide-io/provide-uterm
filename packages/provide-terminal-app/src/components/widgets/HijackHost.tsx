//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { DeckMux } from "@provide-terminal-frontend/app/deckmux/deckmux";
import type { ProvideHijackInstance } from "@provide-terminal-frontend/server-common";
import { useEffect, useRef } from "react";
import type { SessionSurface } from "../../api/types";
import { useTerminalStore } from "../../stores/terminalStore";

interface HijackHostProps {
  sessionId: string;
  surface?: SessionSurface;
}

export function HijackHost({ sessionId, surface }: HijackHostProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(false);
  const setMounted = useTerminalStore((s) => s.setMounted);
  const setDimensions = useTerminalStore((s) => s.setDimensions);

  useEffect(() => {
    if (mountedRef.current || !containerRef.current) return;
    const HijackCtor = window.ProvideHijack;
    if (typeof HijackCtor !== "function") {
      setMounted(false, "ProvideHijack is not available — ensure hijack.js is loaded");
      return;
    }
    const isOperator = surface === "operator";
    mountedRef.current = true;
    setMounted(true);

    // DeckMux state — lazily initialised on first presence_sync
    let deckMux: DeckMux | null = null;
    const presenceUsers = new Map<string, { name: string; color: string }>();
    const container = containerRef.current;
    const ownDimsRef = { cols: 0, rows: 0 };
    let myUserId: string | null = null;

    const widget = new HijackCtor(container, {
      workerId: sessionId,
      showAnalysis: isOperator,
      mobileKeys: isOperator,
      onResize: (cols, rows) => {
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
          if (myUser) {
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
    });

    // Forward xterm scroll events → presence_update (normalized to 0-1 fractions)
    container.addEventListener("uterm:scroll", (e) => {
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
    });

    // Forward DeckMux outbound events → WS
    container.addEventListener("deckmux:request_control", () => {
      widget.sendControlMessage({ type: "control_request" });
    });
    container.addEventListener("deckmux:hand_off", () => {
      widget.sendControlMessage({ type: "control_request" });
    });

    // Keepalive: send dims every 15 s so server-side idle pruning can evict dead connections
    const keepaliveInterval = setInterval(() => {
      if (ownDimsRef.cols > 0) {
        widget.sendControlMessage({ type: "presence_update", cols: ownDimsRef.cols, rows: ownDimsRef.rows });
      }
    }, 15_000);

    return () => clearInterval(keepaliveInterval);
  }, [sessionId, surface, setMounted, setDimensions]);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}
