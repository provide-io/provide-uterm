//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { DeckMuxConfig, DeckMuxUser } from "./types.js";
import "./presence-bar-element.js";
import type { PresenceBar, PresenceUser } from "./presence-bar-element.js";

const IDLE_TIMEOUT_MS = 30_000;

interface AvatarEntry {
  user: PresenceUser;
  idleTimer: ReturnType<typeof setTimeout> | null;
}

export class DeckMuxPresenceBar {
  private readonly _container: HTMLElement;
  private _el: PresenceBar;
  private _entries = new Map<string, AvatarEntry>();
  private _ownerId: string | null = null;

  onAvatarClick: ((userId: string) => void) | null = null;
  onToggleNames: ((visible: boolean) => void) | null = null;
  onToggleCursors: ((visible: boolean) => void) | null = null;
  onToggleGhostBox: ((visible: boolean) => void) | null = null;
  onAvatarHover: ((userId: string) => void) | null = null;
  onAvatarHoverOut: ((userId: string) => void) | null = null;

  // config is reserved for future feature flags (e.g. autoTransferIdleS display)
  constructor(container: HTMLElement, _config: DeckMuxConfig) {
    this._container = container;
    this._el = document.createElement("uterm-presence-bar") as PresenceBar;

    this._el.addEventListener("presence:toggle-names", (e: Event) => {
      const customEvent = e as CustomEvent<boolean>;
      this.onToggleNames?.(customEvent.detail);
    });
    this._el.addEventListener("presence:toggle-cursors", (e: Event) => {
      const customEvent = e as CustomEvent<boolean>;
      this.onToggleCursors?.(customEvent.detail);
    });
    this._el.addEventListener("presence:toggle-ghost-box", (e: Event) => {
      const customEvent = e as CustomEvent<boolean>;
      this.onToggleGhostBox?.(customEvent.detail);
    });
    this._el.addEventListener("presence:click-avatar", (e: Event) => {
      const customEvent = e as CustomEvent<string>;
      this.onAvatarClick?.(customEvent.detail);
    });
    this._el.addEventListener("presence:hover-avatar", (e: Event) => {
      const customEvent = e as CustomEvent<string>;
      this.onAvatarHover?.(customEvent.detail);
    });
    this._el.addEventListener("presence:hover-out-avatar", (e: Event) => {
      const customEvent = e as CustomEvent<string>;
      this.onAvatarHoverOut?.(customEvent.detail);
    });

    this._container.appendChild(this._el);
  }

  private _syncLitElement(): void {
    // Array.from creates a new array reference so Lit triggers an update
    this._el.users = Array.from(this._entries.values()).map(e => e.user);
    this._el.ownerId = this._ownerId;
  }

  addUser(user: DeckMuxUser): void {
    if (this._entries.has(user.userId)) {
      this.updateUser(user.userId, user);
      return;
    }

    this._entries.set(user.userId, { user: { ...user, idle: false, requesting: false }, idleTimer: null });
    this._startIdleTimer(user.userId);
    this._syncLitElement();
  }

  removeUser(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    if (entry.idleTimer !== null) clearTimeout(entry.idleTimer);
    this._entries.delete(userId);
    if (this._ownerId === userId) this._ownerId = null;
    this._syncLitElement();
  }

  updateUser(userId: string, fields: Partial<DeckMuxUser>): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    Object.assign(entry.user, fields);
    this._resetIdleTimer(userId);
    this._syncLitElement();
  }

  setOwner(userId: string): void {
    this._ownerId = userId;
    this._syncLitElement();
  }

  clearOwner(): void {
    this._ownerId = null;
    this._syncLitElement();
  }

  setUserTyping(userId: string, typing: boolean): void {
    this.updateUser(userId, { typing });
  }

  setUserIdle(userId: string, idle: boolean): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    entry.user.idle = idle;
    this._syncLitElement();
  }

  setUserRequesting(userId: string, requesting: boolean): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    entry.user.requesting = requesting;
    this._syncLitElement();
  }

  /** Return the avatar wrapper element for a user, or null if not present. */
  getAvatarElement(userId: string): HTMLElement | null {
    return this._el.getAvatarElement(userId);
  }

  destroy(): void {
    for (const entry of this._entries.values()) {
      if (entry.idleTimer !== null) clearTimeout(entry.idleTimer);
    }
    this._entries.clear();
    this._el.remove();
  }

  private _startIdleTimer(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    if (entry.idleTimer !== null) clearTimeout(entry.idleTimer);
    entry.idleTimer = setTimeout(() => {
      entry.idleTimer = null;
      this.setUserIdle(userId, true);
    }, IDLE_TIMEOUT_MS);
  }

  private _resetIdleTimer(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    entry.user.idle = false;
    this._syncLitElement(); // in case it was idle
    this._startIdleTimer(userId);
  }
}
