//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
const IDLE_TIMEOUT_MS = 30000;
const ROLE_COLORS = {
    admin: "#f97316",
    operator: "#3b82f6",
    viewer: "#6b7280",
};
export class DeckMuxPresenceBar {
    // config is reserved for future feature flags (e.g. autoTransferIdleS display)
    constructor(container, _config) {
        this._root = null;
        this._avatarRow = null;
        this._countBadge = null;
        this._namesVisible = false;
        this._cursorsVisible = true;
        this._ghostBoxVisible = true;
        this._ownerId = null;
        this._entries = new Map();
        this.onAvatarClick = null;
        this.onToggleNames = null;
        this.onToggleCursors = null;
        this.onToggleGhostBox = null;
        this.onAvatarHover = null;
        this.onAvatarHoverOut = null;
        this._container = container;
        this._build();
    }
    _build() {
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
        togglesRow.setAttribute("role", "toolbar");
        togglesRow.setAttribute("aria-label", "Presence display options");
        const namesBtn = document.createElement("button");
        namesBtn.type = "button";
        namesBtn.className = "dm-toggle-btn";
        namesBtn.textContent = "Names";
        namesBtn.setAttribute("aria-pressed", "false");
        namesBtn.setAttribute("aria-label", "Toggle participant names");
        namesBtn.addEventListener("click", () => {
            this._namesVisible = !this._namesVisible;
            namesBtn.setAttribute("aria-pressed", String(this._namesVisible));
            namesBtn.classList.toggle("dm-toggle-btn--active", this._namesVisible);
            this._updateAllNameLabels();
            this.onToggleNames?.(this._namesVisible);
        });
        const cursorsBtn = document.createElement("button");
        cursorsBtn.type = "button";
        cursorsBtn.className = "dm-toggle-btn dm-toggle-btn--active";
        cursorsBtn.textContent = "Cursors";
        cursorsBtn.setAttribute("aria-pressed", "true");
        cursorsBtn.setAttribute("aria-label", "Toggle participant cursors");
        cursorsBtn.addEventListener("click", () => {
            this._cursorsVisible = !this._cursorsVisible;
            cursorsBtn.setAttribute("aria-pressed", String(this._cursorsVisible));
            cursorsBtn.classList.toggle("dm-toggle-btn--active", this._cursorsVisible);
            this.onToggleCursors?.(this._cursorsVisible);
        });
        const dimsBtn = document.createElement("button");
        dimsBtn.type = "button";
        dimsBtn.className = "dm-toggle-btn dm-toggle-btn--active";
        dimsBtn.textContent = "Dims";
        dimsBtn.setAttribute("aria-pressed", "true");
        dimsBtn.setAttribute("aria-label", "Toggle participant viewport dimensions");
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
    addUser(user) {
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
    removeUser(userId) {
        const entry = this._entries.get(userId);
        if (!entry)
            return;
        if (entry.idleTimer !== null)
            clearTimeout(entry.idleTimer);
        entry.el.remove();
        this._entries.delete(userId);
        if (this._ownerId === userId)
            this._ownerId = null;
        this._updateCount();
    }
    updateUser(userId, fields) {
        const entry = this._entries.get(userId);
        if (!entry)
            return;
        Object.assign(entry.user, fields);
        this._syncAvatar(entry);
        this._resetIdleTimer(userId);
    }
    setOwner(userId) {
        const prev = this._ownerId;
        this._ownerId = userId;
        if (prev && prev !== userId) {
            const prevEntry = this._entries.get(prev);
            if (prevEntry)
                this._syncAvatar(prevEntry);
        }
        const entry = this._entries.get(userId);
        if (entry)
            this._syncAvatar(entry);
    }
    clearOwner() {
        const prev = this._ownerId;
        this._ownerId = null;
        if (prev) {
            const entry = this._entries.get(prev);
            if (entry)
                this._syncAvatar(entry);
        }
    }
    setUserTyping(userId, typing) {
        this.updateUser(userId, { typing });
    }
    setUserIdle(userId, idle) {
        const entry = this._entries.get(userId);
        if (!entry)
            return;
        entry.el.classList.toggle("dm-avatar--idle", idle);
    }
    setUserRequesting(userId, requesting) {
        const entry = this._entries.get(userId);
        if (!entry)
            return;
        entry.el.classList.toggle("dm-avatar--requesting", requesting);
    }
    destroy() {
        for (const entry of this._entries.values()) {
            if (entry.idleTimer !== null)
                clearTimeout(entry.idleTimer);
        }
        this._entries.clear();
        this._root?.remove();
        this._root = null;
    }
    _buildAvatar(user) {
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
        roleDot.style.background = ROLE_COLORS[user.role] ?? ROLE_COLORS.viewer;
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
    _syncAvatar(entry) {
        const { user, el } = entry;
        const circle = el.querySelector(".dm-avatar");
        if (circle)
            circle.style.setProperty("--dm-user-color", user.color);
        const initials = el.querySelector(".dm-avatar-initials");
        if (initials)
            initials.textContent = user.initials.slice(0, 2);
        const roleDot = el.querySelector(".dm-role-dot");
        if (roleDot)
            roleDot.style.background = ROLE_COLORS[user.role] ?? ROLE_COLORS.viewer;
        const nameLabel = el.querySelector(".dm-avatar-name");
        if (nameLabel) {
            nameLabel.textContent = user.name;
            nameLabel.style.display = this._namesVisible ? "" : "none";
        }
        const dimsBadge = el.querySelector(".dm-avatar-dims");
        if (dimsBadge && user.cols > 0 && user.rows > 0) {
            dimsBadge.textContent = `${user.rows}×${user.cols}`;
            dimsBadge.style.display = "";
        }
        else if (dimsBadge) {
            dimsBadge.style.display = "none";
        }
        this._applyAvatarState(el, user);
    }
    _applyAvatarState(el, user) {
        const isOwner = this._ownerId === user.userId;
        el.classList.toggle("dm-avatar-wrap--owner", isOwner);
        el.classList.toggle("dm-avatar-wrap--typing", user.typing);
        el.classList.toggle("dm-avatar-wrap--requesting", false);
    }
    _updateAllNameLabels() {
        for (const entry of this._entries.values()) {
            const label = entry.el.querySelector(".dm-avatar-name");
            if (label)
                label.style.display = this._namesVisible ? "" : "none";
        }
    }
    _updateCount() {
        if (!this._countBadge)
            return;
        const count = this._entries.size;
        if (count === 0) {
            this._countBadge.style.display = "none";
        }
        else {
            this._countBadge.style.display = "";
            this._countBadge.textContent = `${count} watching`;
        }
    }
    _startIdleTimer(userId) {
        const entry = this._entries.get(userId);
        if (!entry)
            return;
        if (entry.idleTimer !== null)
            clearTimeout(entry.idleTimer);
        entry.idleTimer = setTimeout(() => {
            entry.idleTimer = null;
            this.setUserIdle(userId, true);
        }, IDLE_TIMEOUT_MS);
    }
    _resetIdleTimer(userId) {
        const entry = this._entries.get(userId);
        if (!entry)
            return;
        entry.el.classList.remove("dm-avatar--idle");
        this._startIdleTimer(userId);
    }
}
