//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { DeckMuxControlPanel } from "./control-panel.js";
import { DeckMuxCursorOverlay } from "./cursor-overlay.js";
import { DeckMuxEdgeIndicators } from "./edge-indicators.js";
import { DeckMuxGhostOverlay } from "./ghost-overlay.js";
import { DeckMuxPresenceBar } from "./presence-bar.js";
import type { ContextAction, DeckMuxConfig, DeckMuxUser } from "./types.js";

/**
 * DeckMux — Main coordinator for collaborative terminal presence.
 *
 * Ties together the presence bar, edge indicators, cursor overlay, and
 * control panel. Consumes WebSocket presence messages and updates all
 * sub-components.
 *
 * Wire up by calling `enable(config)` and piping incoming WS messages
 * through `handleMessage(msg)`.
 */
export class DeckMux {
  private readonly _terminalContainer: HTMLElement;
  private _config: DeckMuxConfig | null = null;
  private _presenceBar: DeckMuxPresenceBar | null = null;
  private _edgeIndicators: DeckMuxEdgeIndicators | null = null;
  private _cursorOverlay: DeckMuxCursorOverlay | null = null;
  private _ghostOverlay: DeckMuxGhostOverlay | null = null;
  private _controlPanel: DeckMuxControlPanel | null = null;
  private _enabled = false;
  private _users = new Map<string, DeckMuxUser>();
  private _myUserId: string | null = null;
  private _barContainer: HTMLElement | null = null;
  private _ownCols = 0;
  private _ownRows = 0;

  // wsConnection is reserved for future direct WS dispatch; coordination currently
  // uses CustomEvent bubbling on the terminal container.
  constructor(terminalContainer: HTMLElement, _wsConnection: unknown) {
    this._terminalContainer = terminalContainer;
  }

  enable(config: DeckMuxConfig): void {
    if (this._enabled) return;
    this._enabled = true;
    this._config = config;

    // Build a bar container above the terminal
    const barContainer = document.createElement("div");
    barContainer.className = "dm-bar-host";
    this._barContainer = barContainer;
    const parent = this._terminalContainer.parentElement ?? this._terminalContainer;
    parent.insertBefore(barContainer, this._terminalContainer);

    this._presenceBar = new DeckMuxPresenceBar(barContainer, config);
    this._edgeIndicators = new DeckMuxEdgeIndicators(this._terminalContainer);
    this._cursorOverlay = new DeckMuxCursorOverlay(this._terminalContainer);
    this._controlPanel = new DeckMuxControlPanel(this._terminalContainer);

    if (config.ghostBox !== false) {
      this._ghostOverlay = new DeckMuxGhostOverlay(this._terminalContainer);
    }

    this._presenceBar.onAvatarClick = (userId) => this._handleAvatarClick(userId);
    this._presenceBar.onToggleNames = (visible) => this._edgeIndicators?.setNamesVisible(visible);
    this._presenceBar.onToggleCursors = (visible) => this._cursorOverlay?.setVisible(visible);
    this._presenceBar.onToggleGhostBox = (visible) => this._ghostOverlay?.setVisible(visible);
    this._presenceBar.onAvatarHover = (userId) => this._handleAvatarHover(userId);
    this._presenceBar.onAvatarHoverOut = (userId) => this._handleAvatarHoverOut(userId);
  }

  disable(): void {
    if (!this._enabled) return;
    this._enabled = false;
    this._teardown();
  }

  setOwnDimensions(cols: number, rows: number): void {
    this._ownCols = cols;
    this._ownRows = rows;
    this._ghostOverlay?.setOwnDimensions(cols, rows);
  }

  handleMessage(msg: Record<string, unknown>): void {
    if (!this._enabled) return;
    const type = msg.type;
    if (typeof type !== "string") return;

    switch (type) {
      case "dm_hello":
        this._handleHello(msg);
        break;
      case "dm_join":
        this._handleJoin(msg);
        break;
      case "dm_leave":
        this._handleLeave(msg);
        break;
      case "dm_presence":
        this._handlePresence(msg);
        break;
      case "dm_owner_change":
        this._handleOwnerChange(msg);
        break;
      case "dm_control_request":
        this._handleControlRequest(msg);
        break;
      case "dm_control_transfer":
        this._handleControlTransfer(msg);
        break;
      case "dm_auto_transfer_warning":
        this._handleAutoTransferWarning(msg);
        break;
      case "dm_snapshot":
        this._handleSnapshot(msg);
        break;
      default:
        break;
    }
  }

  destroy(): void {
    this._teardown();
    this._barContainer?.remove();
    this._barContainer = null;
  }

  private _teardown(): void {
    this._presenceBar?.destroy();
    this._edgeIndicators?.destroy();
    this._cursorOverlay?.destroy();
    this._ghostOverlay?.destroy();
    this._controlPanel?.destroy();
    this._presenceBar = null;
    this._edgeIndicators = null;
    this._cursorOverlay = null;
    this._ghostOverlay = null;
    this._controlPanel = null;
    this._users.clear();
  }

  private _handleAvatarHover(userId: string): void {
    const user = this._users.get(userId);
    if (!user || user.cols === 0 || user.rows === 0) return;
    this._ghostOverlay?.showUser(userId, user.color, user.cols, user.rows);
  }

  private _handleAvatarHoverOut(userId: string): void {
    this._ghostOverlay?.hideUser(userId);
  }

  private _handleHello(msg: Record<string, unknown>): void {
    const userId = msg.user_id;
    if (typeof userId === "string") this._myUserId = userId;
  }

  private _handleJoin(msg: Record<string, unknown>): void {
    const user = this._extractUser(msg);
    if (!user) return;
    this._users.set(user.userId, user);
    this._presenceBar?.addUser(user);
    this._updateEdge(user);
  }

  private _handleLeave(msg: Record<string, unknown>): void {
    const userId = msg.user_id;
    if (typeof userId !== "string") return;
    this._users.delete(userId);
    this._presenceBar?.removeUser(userId);
    this._edgeIndicators?.removeUser(userId);
    this._cursorOverlay?.removePin(userId);
    this._cursorOverlay?.removeSelection(userId);
    this._ghostOverlay?.removeUser(userId);
  }

  private _handlePresence(msg: Record<string, unknown>): void {
    const userId = msg.user_id;
    if (typeof userId !== "string") return;
    const existing = this._users.get(userId);
    if (!existing) return;

    const fields = this._extractPartialUser(msg);
    const prevCols = existing.cols;
    const prevRows = existing.rows;
    Object.assign(existing, fields);
    this._presenceBar?.updateUser(userId, fields);
    this._updateEdge(existing);

    if (existing.cols > 0 && existing.rows > 0 && (existing.cols !== prevCols || existing.rows !== prevRows)) {
      this._ghostOverlay?.flashUser(existing.userId, existing.color, existing.cols, existing.rows);
    }

    if (existing.typing !== undefined) {
      this._presenceBar?.setUserTyping(userId, existing.typing);
    }

    if (existing.pin) {
      this._cursorOverlay?.setPin(userId, existing.pin.line, existing.name, existing.color, existing.isOwner);
    } else {
      this._cursorOverlay?.removePin(userId);
    }

    if (existing.selection) {
      this._cursorOverlay?.setSelection(
        userId,
        existing.selection.start.line,
        existing.selection.end.line,
        existing.color,
      );
    } else {
      this._cursorOverlay?.removeSelection(userId);
    }

    if (typeof msg.queued_keys === "string" && this._config?.keystrokeQueue === "display") {
      const pos = this._avatarPosition(userId);
      if (pos) this._controlPanel?.showKeystrokeQueue(userId, msg.queued_keys as string, pos);
    }
  }

  private _handleOwnerChange(msg: Record<string, unknown>): void {
    const userId = msg.user_id;
    if (typeof userId === "string") {
      this._presenceBar?.setOwner(userId);
      // Update isOwner flags
      for (const [uid, user] of this._users) {
        user.isOwner = uid === userId;
      }
    } else {
      this._presenceBar?.clearOwner();
    }
  }

  private _handleControlRequest(msg: Record<string, unknown>): void {
    const fromId = msg.from_user_id;
    const fromName = msg.from_name;
    const fromColor = msg.from_color;
    if (typeof fromId !== "string" || typeof fromName !== "string" || typeof fromColor !== "string") return;

    this._presenceBar?.setUserRequesting(fromId, true);

    // Only the current owner sees the accept/deny toast
    const myUser = this._myUserId ? this._users.get(this._myUserId) : null;
    if (!myUser?.isOwner) return;

    this._controlPanel?.showRequestToast(
      fromName,
      fromColor,
      () => this._sendControlResponse(fromId, "accept"),
      () => this._sendControlResponse(fromId, "deny"),
    );
  }

  private _handleControlTransfer(msg: Record<string, unknown>): void {
    const toName = msg.to_name;
    const toColor = msg.to_color;
    const toId = msg.to_user_id;
    if (typeof toName !== "string" || typeof toColor !== "string" || typeof toId !== "string") return;

    this._presenceBar?.setUserRequesting(toId, false);
    this._controlPanel?.showTransferToast(toName, toColor);
  }

  private _handleAutoTransferWarning(msg: Record<string, unknown>): void {
    const toName = msg.to_name;
    const seconds = msg.seconds_remaining;
    const complete = msg.complete;

    if (complete === true && typeof toName === "string") {
      this._controlPanel?.showAutoTransferComplete(toName);
      return;
    }

    if (typeof toName === "string" && typeof seconds === "number") {
      this._controlPanel?.showAutoTransferWarning(toName, seconds);
    }
  }

  private _handleSnapshot(msg: Record<string, unknown>): void {
    const users = msg.users;
    if (!Array.isArray(users)) return;
    for (const raw of users) {
      if (typeof raw !== "object" || raw === null) continue;
      const user = this._extractUser(raw as Record<string, unknown>);
      if (!user) continue;
      if (this._users.has(user.userId)) {
        this._users.set(user.userId, user);
        this._presenceBar?.updateUser(user.userId, user);
      } else {
        this._users.set(user.userId, user);
        this._presenceBar?.addUser(user);
      }
      this._updateEdge(user);
    }

    const ownerId = msg.owner_id;
    if (typeof ownerId === "string") {
      this._presenceBar?.setOwner(ownerId);
    }
  }

  private _extractUser(msg: Record<string, unknown>): DeckMuxUser | null {
    const userId = msg.user_id;
    const name = msg.name;
    const color = msg.color;
    const role = msg.role;
    if (typeof userId !== "string" || typeof name !== "string" || typeof color !== "string") return null;

    return {
      userId,
      name,
      color,
      role: typeof role === "string" ? role : "viewer",
      initials: this._initials(name),
      scrollLine: typeof msg.scroll_line === "number" ? (msg.scroll_line as number) : 0,
      scrollRange: Array.isArray(msg.scroll_range) ? (msg.scroll_range as [number, number]) : [0, 1],
      cols: typeof msg.cols === "number" ? (msg.cols as number) : 0,
      rows: typeof msg.rows === "number" ? (msg.rows as number) : 0,
      joinTime: Date.now(),
      selection: this._extractSelection(msg.selection),
      pin: this._extractPin(msg.pin),
      typing: msg.typing === true,
      queuedKeys: typeof msg.queued_keys === "string" ? (msg.queued_keys as string) : "",
      isOwner: msg.is_owner === true,
    };
  }

  private _extractPartialUser(msg: Record<string, unknown>): Partial<DeckMuxUser> {
    const fields: Partial<DeckMuxUser> = {};
    if (typeof msg.name === "string") fields.name = msg.name as string;
    if (typeof msg.color === "string") fields.color = msg.color as string;
    if (typeof msg.role === "string") fields.role = msg.role as string;
    if (typeof msg.scroll_line === "number") fields.scrollLine = msg.scroll_line as number;
    if (Array.isArray(msg.scroll_range)) fields.scrollRange = msg.scroll_range as [number, number];
    if ("selection" in msg) fields.selection = this._extractSelection(msg.selection);
    if ("pin" in msg) fields.pin = this._extractPin(msg.pin);
    if (typeof msg.typing === "boolean") fields.typing = msg.typing as boolean;
    if (typeof msg.queued_keys === "string") fields.queuedKeys = msg.queued_keys as string;
    if (typeof msg.is_owner === "boolean") fields.isOwner = msg.is_owner as boolean;
    if (typeof msg.cols === "number") fields.cols = msg.cols as number;
    if (typeof msg.rows === "number") fields.rows = msg.rows as number;
    if (fields.name) fields.initials = this._initials(fields.name);
    return fields;
  }

  private _extractSelection(
    raw: unknown,
  ): { start: { line: number; col: number }; end: { line: number; col: number } } | null {
    if (!raw || typeof raw !== "object") return null;
    const r = raw as Record<string, unknown>;
    const start = r.start as Record<string, unknown> | undefined;
    const end = r.end as Record<string, unknown> | undefined;
    if (!start || !end) return null;
    return {
      start: { line: Number(start.line ?? 0), col: Number(start.col ?? 0) },
      end: { line: Number(end.line ?? 0), col: Number(end.col ?? 0) },
    };
  }

  private _extractPin(raw: unknown): { line: number } | null {
    if (!raw || typeof raw !== "object") return null;
    const r = raw as Record<string, unknown>;
    if (typeof r.line !== "number") return null;
    return { line: r.line as number };
  }

  private _updateEdge(user: DeckMuxUser): void {
    const [top, bottom] = user.scrollRange;
    const height = Math.max(0.01, bottom - top);
    const edgeOptions: {
      isOwner?: boolean;
      selection?: { top: number; height: number };
      pin?: number;
      name?: string;
      idle?: boolean;
    } = { isOwner: user.isOwner, name: user.name, idle: false };
    if (user.pin !== null) edgeOptions.pin = user.pin.line;
    if (user.selection !== null) {
      edgeOptions.selection = {
        top: user.selection.start.line / 1000,
        height: (user.selection.end.line - user.selection.start.line + 1) / 1000,
      };
    }
    this._edgeIndicators?.setUser(user.userId, user.color, { top, height }, edgeOptions);
  }

  private _avatarPosition(_userId: string): { x: number; y: number } | null {
    // Position near the bar; a real implementation would query the avatar element.
    const barRect = this._barContainer?.getBoundingClientRect();
    if (!barRect) return null;
    return { x: barRect.left + 8, y: barRect.bottom + 4 };
  }

  private _sendControlResponse(toUserId: string, response: "accept" | "deny"): void {
    this._presenceBar?.setUserRequesting(toUserId, false);
    // The actual send is handled by the integration layer that owns the WS
    // connection. We expose the wsConnection reference so callers can set a
    // delegate, but for now the message is dispatched as a custom DOM event
    // on the terminal container so the host app can intercept it.
    const event = new CustomEvent("deckmux:control_response", {
      bubbles: true,
      detail: { toUserId, response },
    });
    this._terminalContainer.dispatchEvent(event);
  }

  private _handleAvatarClick(userId: string): void {
    const user = this._users.get(userId);
    if (!user) return;

    const avatarEl = this._barContainer?.querySelector(`[data-user-id="${userId}"]`);
    const rect = avatarEl?.getBoundingClientRect();
    const pos = rect ? { x: rect.left, y: rect.bottom + 4 } : { x: 0, y: 0 };

    const actions: ContextAction[] = [
      {
        icon: "↗",
        label: "Jump to view",
        onClick: () => {
          const event = new CustomEvent("deckmux:jump_to_view", { bubbles: true, detail: { userId } });
          this._terminalContainer.dispatchEvent(event);
        },
      },
    ];

    const myUser = this._myUserId ? this._users.get(this._myUserId) : null;
    if (myUser?.isOwner && !user.isOwner) {
      actions.push({
        icon: "→",
        label: "Hand off control",
        sublabel: `Transfer to ${user.name}`,
        onClick: () => {
          const event = new CustomEvent("deckmux:hand_off", { bubbles: true, detail: { toUserId: userId } });
          this._terminalContainer.dispatchEvent(event);
        },
      });
    }

    if (!myUser?.isOwner && user.isOwner) {
      actions.push({
        icon: "⚡",
        label: "Request control",
        onClick: () => {
          const event = new CustomEvent("deckmux:request_control", { bubbles: true, detail: {} });
          this._terminalContainer.dispatchEvent(event);
        },
      });
    }

    const noOwner = !Array.from(this._users.values()).some((u) => u.isOwner);
    if (noOwner && userId === this._myUserId) {
      actions.push({
        icon: "👑",
        label: "Claim control",
        onClick: () => {
          const event = new CustomEvent("deckmux:request_control", { bubbles: true, detail: {} });
          this._terminalContainer.dispatchEvent(event);
        },
      });
    }

    if (myUser?.isOwner && userId !== this._myUserId) {
      actions.push({
        icon: "✕",
        label: "Kick user",
        danger: true,
        onClick: () => {
          const event = new CustomEvent("deckmux:kick_user", { bubbles: true, detail: { userId } });
          this._terminalContainer.dispatchEvent(event);
        },
      });
    }

    this._controlPanel?.showContextMenu(userId, user, pos, actions);
  }

  private _initials(name: string): string {
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
      return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase();
    }
    return name.slice(0, 2).toUpperCase();
  }
}
