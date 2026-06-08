//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { DeckMux } from "../deckmux/deckmux.js";

type SessionSurface = "user" | "operator";

interface WidgetMountState {
  mounted: boolean;
  error: string | null;
}

function widgetSurface(surface: SessionSurface | undefined): { showAnalysis: boolean; mobileKeys: boolean } {
  const isOperator = surface === "operator";
  return { showAnalysis: isOperator, mobileKeys: isOperator };
}

export function mountHijackWidget(
  container: HTMLElement,
  sessionId: string,
  surface: SessionSurface | undefined,
): WidgetMountState {
  const HijackWidget = window.ProvideHijack;
  if (typeof HijackWidget !== "function") {
    return { mounted: false, error: "ProvideHijack is not available" };
  }
  const widgetConfig = widgetSurface(surface);

  // DeckMux state — lazily initialised on first presence_sync
  let deckMux: DeckMux | null = null;
  let myUserId: string | null = null;
  const presenceUsers = new Map<string, { name: string; color: string }>();

  const config: {
    workerId: string;
    showAnalysis?: boolean;
    mobileKeys?: boolean;
    authToken?: string;
    onPresenceMessage?: (msg: Record<string, unknown>) => void;
  } = {
    workerId: sessionId,
    showAnalysis: widgetConfig.showAnalysis,
    mobileKeys: widgetConfig.mobileKeys,
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
          });
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
        widget.sendControlMessage({ type: "presence_update", scroll_line: 0, scroll_range: [0, 25] });
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

  const widget = new HijackWidget(container, config);

  // Forward xterm scroll → presence_update to server (debounced to reduce WS traffic)
  let scrollTimer: ReturnType<typeof setTimeout> | null = null;
  container.addEventListener("uterm:scroll", (e) => {
    const { viewportY, rows, totalLines } = (e as CustomEvent<{ viewportY: number; rows: number; totalLines: number }>)
      .detail;
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      widget.sendControlMessage({
        type: "presence_update",
        scroll_line: viewportY,
        scroll_range: [viewportY, viewportY + rows],
        total_lines: totalLines,
      });
    }, 200);
  });

  // Forward DeckMux outbound control events → WS
  container.addEventListener("deckmux:request_control", () => {
    widget.sendControlMessage({ type: "control_request" });
  });
  container.addEventListener("deckmux:hand_off", () => {
    widget.sendControlMessage({ type: "control_request" });
  });

  return { mounted: true, error: null };
}
