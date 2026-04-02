//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

interface PinEntry {
  line: number;
  name: string;
  color: string;
  isOwner: boolean;
  el: HTMLElement;
}

interface SelectionEntry {
  startLine: number;
  endLine: number;
  color: string;
  el: HTMLElement;
}

export class DeckMuxCursorOverlay {
  private readonly _terminalContainer: HTMLElement;
  private _overlay: HTMLElement | null = null;
  private _visible = true;
  private _pins = new Map<string, PinEntry>();
  private _selections = new Map<string, SelectionEntry>();

  constructor(terminalContainer: HTMLElement) {
    this._terminalContainer = terminalContainer;
    this._buildOverlay();
  }

  private _buildOverlay(): void {
    const overlay = document.createElement("div");
    overlay.className = "dm-cursor-overlay";
    this._overlay = overlay;
    this._terminalContainer.appendChild(overlay);
  }

  setPin(userId: string, line: number, name: string, color: string, isOwner: boolean): void {
    const existing = this._pins.get(userId);
    if (existing) {
      existing.line = line;
      existing.name = name;
      existing.color = color;
      existing.isOwner = isOwner;
      this._syncPin(existing);
    } else {
      const el = document.createElement("div");
      el.className = "dm-pin";
      el.dataset.userId = userId;
      const entry: PinEntry = { line, name, color, isOwner, el };
      this._pins.set(userId, entry);
      this._overlay?.appendChild(el);
      this._syncPin(entry);
    }
    this._applyVisibility();
  }

  removePin(userId: string): void {
    const entry = this._pins.get(userId);
    if (!entry) return;
    entry.el.remove();
    this._pins.delete(userId);
  }

  setSelection(userId: string, startLine: number, endLine: number, color: string): void {
    const existing = this._selections.get(userId);
    if (existing) {
      existing.startLine = startLine;
      existing.endLine = endLine;
      existing.color = color;
      this._syncSelection(existing);
    } else {
      const el = document.createElement("div");
      el.className = "dm-selection";
      el.dataset.userId = userId;
      const entry: SelectionEntry = { startLine, endLine, color, el };
      this._selections.set(userId, entry);
      this._overlay?.appendChild(el);
      this._syncSelection(entry);
    }
    this._applyVisibility();
  }

  removeSelection(userId: string): void {
    const entry = this._selections.get(userId);
    if (!entry) return;
    entry.el.remove();
    this._selections.delete(userId);
  }

  setVisible(visible: boolean): void {
    this._visible = visible;
    this._applyVisibility();
  }

  destroy(): void {
    this._overlay?.remove();
    this._overlay = null;
    this._pins.clear();
    this._selections.clear();
  }

  private _syncPin(entry: PinEntry): void {
    const { el, line, name, color, isOwner } = entry;
    el.style.setProperty("--dm-user-color", color);
    el.style.top = `${line}lh`;
    el.classList.toggle("dm-pin--owner", isOwner);

    const icon = isOwner ? "\u2328\ufe0f" : "\uD83D\uDCCC";
    const label = el.querySelector(".dm-pin-label") ?? document.createElement("span");
    label.className = "dm-pin-label";
    label.textContent = `${icon} ${name}`;
    if (!el.contains(label)) el.appendChild(label);

    const bar = el.querySelector(".dm-pin-bar") ?? document.createElement("div");
    bar.className = "dm-pin-bar";
    if (!el.contains(bar)) el.prepend(bar);
  }

  private _syncSelection(entry: SelectionEntry): void {
    const { el, startLine, endLine, color } = entry;
    const lineCount = Math.max(1, endLine - startLine + 1);
    el.style.setProperty("--dm-user-color", color);
    el.style.top = `${startLine}lh`;
    el.style.height = `${lineCount}lh`;
  }

  private _applyVisibility(): void {
    if (!this._overlay) return;
    this._overlay.style.display = this._visible ? "" : "none";
  }
}
