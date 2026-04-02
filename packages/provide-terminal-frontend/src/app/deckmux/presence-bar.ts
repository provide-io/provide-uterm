//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { DeckMuxConfig, DeckMuxUser } from "./types.js";

const IDLE_TIMEOUT_MS = 30_000;
const ROLE_COLORS: Record<string, string> = {
  admin: "#f97316",
  operator: "#3b82f6",
  viewer: "#6b7280",
};

interface AvatarEntry {
  user: DeckMuxUser;
  el: HTMLElement;
  idleTimer: ReturnType<typeof setTimeout> | null;
}

export class DeckMuxPresenceBar {
  private readonly _container: HTMLElement;
  private _root: HTMLElement | null = null;
  private _avatarRow: HTMLElement | null = null;
  private _countBadge: HTMLElement | null = null;
  private _namesVisible = false;
  private _cursorsVisible = true;
  private _ghostBoxVisible = true;
  private _ownerId: string | null = null;
  private _entries = new Map<string, AvatarEntry>();

  onAvatarClick: ((userId: string) => void) | null = null;
  onToggleNames: ((visible: boolean) => void) | null = null;
  onToggleCursors: ((visible: boolean) => void) | null = null;
  onToggleGhostBox: ((visible: boolean) => void) | null = null;
  onAvatarHover: ((userId: string) => void) | null = null;
  onAvatarHoverOut: ((userId: string) => void) | null = null;

  // config is reserved for future feature flags (e.g. autoTransferIdleS display)
  constructor(container: HTMLElement, _config: DeckMuxConfig) {
    this._container = container;
    this._build();
  }

  private _build(): void {
    const root = document.createElement("div");
    root.className = "dm-presence-bar";

    const avatarRow = document.createElement("div");
    avatarRow.className = "dm-avatar-row";
    this._avatarRow = avatarRow;

    const countBadge = document.createElement("span");
    countBadge.className = "dm-count-badge";
    countBadge.style.display = "none";
    this._countBadge = countBadge;

    const togglesRow = document.createElement("div");
    togglesRow.className = "dm-toggles";

    const namesBtn = document.createElement("button");
    namesBtn.className = "dm-toggle-btn";
    namesBtn.textContent = "Names";
    namesBtn.setAttribute("aria-pressed", "false");
    namesBtn.addEventListener("click", () => {
      this._namesVisible = !this._namesVisible;
      namesBtn.setAttribute("aria-pressed", String(this._namesVisible));
      namesBtn.classList.toggle("dm-toggle-btn--active", this._namesVisible);
      this._updateAllNameLabels();
      this.onToggleNames?.(this._namesVisible);
    });

    const cursorsBtn = document.createElement("button");
    cursorsBtn.className = "dm-toggle-btn dm-toggle-btn--active";
    cursorsBtn.textContent = "Cursors";
    cursorsBtn.setAttribute("aria-pressed", "true");
    cursorsBtn.addEventListener("click", () => {
      this._cursorsVisible = !this._cursorsVisible;
      cursorsBtn.setAttribute("aria-pressed", String(this._cursorsVisible));
      cursorsBtn.classList.toggle("dm-toggle-btn--active", this._cursorsVisible);
      this.onToggleCursors?.(this._cursorsVisible);
    });

    const dimsBtn = document.createElement("button");
    dimsBtn.className = "dm-toggle-btn dm-toggle-btn--active";
    dimsBtn.textContent = "Dims";
    dimsBtn.setAttribute("aria-pressed", "true");
    dimsBtn.addEventListener("click", () => {
      this._ghostBoxVisible = !this._ghostBoxVisible;
      dimsBtn.setAttribute("aria-pressed", String(this._ghostBoxVisible));
      dimsBtn.classList.toggle("dm-toggle-btn--active", this._ghostBoxVisible);
      this.onToggleGhostBox?.(this._ghostBoxVisible);
    });

    togglesRow.appendChild(namesBtn);
    togglesRow.appendChild(cursorsBtn);
    togglesRow.appendChild(dimsBtn);

    root.appendChild(avatarRow);
    root.appendChild(countBadge);
    root.appendChild(togglesRow);

    this._root = root;
    this._container.appendChild(root);
  }

  addUser(user: DeckMuxUser): void {
    if (this._entries.has(user.userId)) {
      this.updateUser(user.userId, user);
      return;
    }

    const el = this._buildAvatar(user);
    this._avatarRow?.appendChild(el);
    this._entries.set(user.userId, { user: { ...user }, el, idleTimer: null });
    this._updateCount();
    this._startIdleTimer(user.userId);
  }

  removeUser(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    if (entry.idleTimer !== null) clearTimeout(entry.idleTimer);
    entry.el.remove();
    this._entries.delete(userId);
    if (this._ownerId === userId) this._ownerId = null;
    this._updateCount();
  }

  updateUser(userId: string, fields: Partial<DeckMuxUser>): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    Object.assign(entry.user, fields);
    this._syncAvatar(entry);
    this._resetIdleTimer(userId);
  }

  setOwner(userId: string): void {
    const prev = this._ownerId;
    this._ownerId = userId;
    if (prev && prev !== userId) {
      const prevEntry = this._entries.get(prev);
      if (prevEntry) this._syncAvatar(prevEntry);
    }
    const entry = this._entries.get(userId);
    if (entry) this._syncAvatar(entry);
  }

  clearOwner(): void {
    const prev = this._ownerId;
    this._ownerId = null;
    if (prev) {
      const entry = this._entries.get(prev);
      if (entry) this._syncAvatar(entry);
    }
  }

  setUserTyping(userId: string, typing: boolean): void {
    this.updateUser(userId, { typing });
  }

  setUserIdle(userId: string, idle: boolean): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    entry.el.classList.toggle("dm-avatar--idle", idle);
  }

  setUserRequesting(userId: string, requesting: boolean): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    entry.el.classList.toggle("dm-avatar--requesting", requesting);
  }

  destroy(): void {
    for (const entry of this._entries.values()) {
      if (entry.idleTimer !== null) clearTimeout(entry.idleTimer);
    }
    this._entries.clear();
    this._root?.remove();
    this._root = null;
  }

  private _buildAvatar(user: DeckMuxUser): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "dm-avatar-wrap";
    wrap.dataset.userId = user.userId;

    const circle = document.createElement("div");
    circle.className = "dm-avatar";
    circle.style.setProperty("--dm-user-color", user.color);

    const initials = document.createElement("span");
    initials.className = "dm-avatar-initials";
    initials.textContent = user.initials.slice(0, 2);

    const roleDot = document.createElement("span");
    roleDot.className = "dm-role-dot";
    roleDot.style.background = ROLE_COLORS[user.role] ?? (ROLE_COLORS.viewer as string);

    const typingDot = document.createElement("span");
    typingDot.className = "dm-typing-dot";

    circle.appendChild(initials);
    circle.appendChild(roleDot);
    circle.appendChild(typingDot);

    const nameLabel = document.createElement("span");
    nameLabel.className = "dm-avatar-name";
    nameLabel.textContent = user.name;
    nameLabel.style.display = this._namesVisible ? "" : "none";

    const dimsBadge = document.createElement("span");
    dimsBadge.className = "dm-avatar-dims";
    dimsBadge.style.display = "none";

    wrap.appendChild(circle);
    wrap.appendChild(nameLabel);
    wrap.appendChild(dimsBadge);

    wrap.addEventListener("mouseenter", () => this.onAvatarHover?.(user.userId));
    wrap.addEventListener("mouseleave", () => this.onAvatarHoverOut?.(user.userId));
    wrap.addEventListener("click", () => this.onAvatarClick?.(user.userId));

    this._applyAvatarState(wrap, user);
    return wrap;
  }

  private _syncAvatar(entry: AvatarEntry): void {
    const { user, el } = entry;

    const circle = el.querySelector<HTMLElement>(".dm-avatar");
    if (circle) circle.style.setProperty("--dm-user-color", user.color);

    const initials = el.querySelector<HTMLElement>(".dm-avatar-initials");
    if (initials) initials.textContent = user.initials.slice(0, 2);

    const roleDot = el.querySelector<HTMLElement>(".dm-role-dot");
    if (roleDot) roleDot.style.background = ROLE_COLORS[user.role] ?? (ROLE_COLORS.viewer as string);

    const nameLabel = el.querySelector<HTMLElement>(".dm-avatar-name");
    if (nameLabel) {
      nameLabel.textContent = user.name;
      nameLabel.style.display = this._namesVisible ? "" : "none";
    }

    const dimsBadge = el.querySelector<HTMLElement>(".dm-avatar-dims");
    if (dimsBadge && user.cols > 0 && user.rows > 0) {
      dimsBadge.textContent = `${user.rows}×${user.cols}`;
      dimsBadge.style.display = "";
    } else if (dimsBadge) {
      dimsBadge.style.display = "none";
    }

    this._applyAvatarState(el, user);
  }

  private _applyAvatarState(el: HTMLElement, user: DeckMuxUser): void {
    const isOwner = this._ownerId === user.userId;
    el.classList.toggle("dm-avatar-wrap--owner", isOwner);
    el.classList.toggle("dm-avatar-wrap--typing", user.typing);
    el.classList.toggle("dm-avatar-wrap--requesting", false);
  }

  private _updateAllNameLabels(): void {
    for (const entry of this._entries.values()) {
      const label = entry.el.querySelector<HTMLElement>(".dm-avatar-name");
      if (label) label.style.display = this._namesVisible ? "" : "none";
    }
  }

  private _updateCount(): void {
    if (!this._countBadge) return;
    const count = this._entries.size;
    if (count === 0) {
      this._countBadge.style.display = "none";
    } else {
      this._countBadge.style.display = "";
      this._countBadge.textContent = `${count} watching`;
    }
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
    entry.el.classList.remove("dm-avatar--idle");
    this._startIdleTimer(userId);
  }
}
