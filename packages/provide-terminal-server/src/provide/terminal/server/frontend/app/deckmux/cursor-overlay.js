//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
export class DeckMuxCursorOverlay {
    constructor(terminalContainer) {
        this._overlay = null;
        this._visible = true;
        this._pins = new Map();
        this._selections = new Map();
        this._terminalContainer = terminalContainer;
        this._buildOverlay();
    }
    _buildOverlay() {
        const overlay = document.createElement("div");
        overlay.className = "dm-cursor-overlay";
        this._overlay = overlay;
        this._terminalContainer.appendChild(overlay);
    }
    setPin(userId, line, name, color, isOwner) {
        const existing = this._pins.get(userId);
        if (existing) {
            existing.line = line;
            existing.name = name;
            existing.color = color;
            existing.isOwner = isOwner;
            this._syncPin(existing);
        }
        else {
            const el = document.createElement("div");
            el.className = "dm-pin";
            el.dataset.userId = userId;
            const entry = { line, name, color, isOwner, el };
            this._pins.set(userId, entry);
            this._overlay?.appendChild(el);
            this._syncPin(entry);
        }
        this._applyVisibility();
    }
    removePin(userId) {
        const entry = this._pins.get(userId);
        if (!entry)
            return;
        entry.el.remove();
        this._pins.delete(userId);
    }
    setSelection(userId, startLine, endLine, color) {
        const existing = this._selections.get(userId);
        if (existing) {
            existing.startLine = startLine;
            existing.endLine = endLine;
            existing.color = color;
            this._syncSelection(existing);
        }
        else {
            const el = document.createElement("div");
            el.className = "dm-selection";
            el.dataset.userId = userId;
            const entry = { startLine, endLine, color, el };
            this._selections.set(userId, entry);
            this._overlay?.appendChild(el);
            this._syncSelection(entry);
        }
        this._applyVisibility();
    }
    removeSelection(userId) {
        const entry = this._selections.get(userId);
        if (!entry)
            return;
        entry.el.remove();
        this._selections.delete(userId);
    }
    setVisible(visible) {
        this._visible = visible;
        this._applyVisibility();
    }
    destroy() {
        this._overlay?.remove();
        this._overlay = null;
        this._pins.clear();
        this._selections.clear();
    }
    _syncPin(entry) {
        const { el, line, name, color, isOwner } = entry;
        el.style.setProperty("--dm-user-color", color);
        el.style.top = `${line}lh`;
        el.classList.toggle("dm-pin--owner", isOwner);
        const icon = isOwner ? "\u2328\ufe0f" : "\uD83D\uDCCC";
        const label = el.querySelector(".dm-pin-label") ?? document.createElement("span");
        label.className = "dm-pin-label";
        label.textContent = `${icon} ${name}`;
        if (!el.contains(label))
            el.appendChild(label);
        const bar = el.querySelector(".dm-pin-bar") ?? document.createElement("div");
        bar.className = "dm-pin-bar";
        if (!el.contains(bar))
            el.prepend(bar);
    }
    _syncSelection(entry) {
        const { el, startLine, endLine, color } = entry;
        const lineCount = Math.max(1, endLine - startLine + 1);
        el.style.setProperty("--dm-user-color", color);
        el.style.top = `${startLine}lh`;
        el.style.height = `${lineCount}lh`;
    }
    _applyVisibility() {
        if (!this._overlay)
            return;
        this._overlay.style.display = this._visible ? "" : "none";
    }
}
