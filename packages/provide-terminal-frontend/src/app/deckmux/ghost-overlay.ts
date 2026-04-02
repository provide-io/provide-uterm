//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

const FLASH_DURATION_MS = 1800;

interface GhostEntry {
  el: HTMLElement;
  flashTimer: ReturnType<typeof setTimeout> | null;
}

export class DeckMuxGhostOverlay {
  private readonly _terminalContainer: HTMLElement;
  private _overlay: HTMLElement | null = null;
  private _visible = true;
  private _entries = new Map<string, GhostEntry>();
  // Own terminal dimensions (updated externally)
  private _ownCols = 0;
  private _ownRows = 0;

  constructor(terminalContainer: HTMLElement) {
    this._terminalContainer = terminalContainer;
    this._buildOverlay();
  }

  private _buildOverlay(): void {
    const overlay = document.createElement("div");
    overlay.className = "dm-ghost-overlay";
    this._overlay = overlay;
    this._terminalContainer.appendChild(overlay);
  }

  setOwnDimensions(cols: number, rows: number): void {
    this._ownCols = cols;
    this._ownRows = rows;
    // Re-sync all visible entries
    for (const entry of this._entries.values()) {
      const el = entry.el;
      if (el.dataset.cols && el.dataset.rows) {
        this._positionBox(el, Number(el.dataset.cols), Number(el.dataset.rows));
      }
    }
  }

  /** Show ghost box for a user (on hover). Stays until hideUser is called. */
  showUser(userId: string, color: string, cols: number, rows: number): void {
    if (!this._visible || cols === 0 || rows === 0) return;
    const existing = this._entries.get(userId);
    if (existing) {
      existing.el.dataset.cols = String(cols);
      existing.el.dataset.rows = String(rows);
      existing.el.style.setProperty("--dm-user-color", color);
      this._positionBox(existing.el, cols, rows);
      existing.el.classList.remove("dm-ghost-box--hidden");
      return;
    }
    const el = document.createElement("div");
    el.className = "dm-ghost-box";
    el.dataset.userId = userId;
    el.dataset.cols = String(cols);
    el.dataset.rows = String(rows);
    el.style.setProperty("--dm-user-color", color);
    this._positionBox(el, cols, rows);
    this._overlay?.appendChild(el);
    this._entries.set(userId, { el, flashTimer: null });
  }

  /** Hide the persistent ghost box for a user (on hover-out). */
  hideUser(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    // Only hide if not in flash mode
    if (entry.flashTimer === null) {
      entry.el.classList.add("dm-ghost-box--hidden");
    }
  }

  /** Briefly flash a ghost box when user resizes. */
  flashUser(userId: string, color: string, cols: number, rows: number): void {
    if (!this._visible || cols === 0 || rows === 0) return;
    this.showUser(userId, color, cols, rows);
    const entry = this._entries.get(userId);
    if (!entry) return;
    if (entry.flashTimer !== null) clearTimeout(entry.flashTimer);
    entry.el.classList.add("dm-ghost-box--flash");
    entry.flashTimer = setTimeout(() => {
      entry.el.classList.remove("dm-ghost-box--flash");
      entry.el.classList.add("dm-ghost-box--hidden");
      entry.flashTimer = null;
    }, FLASH_DURATION_MS);
  }

  removeUser(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    if (entry.flashTimer !== null) clearTimeout(entry.flashTimer);
    entry.el.remove();
    this._entries.delete(userId);
  }

  setVisible(visible: boolean): void {
    this._visible = visible;
    if (!visible) {
      for (const entry of this._entries.values()) {
        entry.el.classList.add("dm-ghost-box--hidden");
      }
    }
  }

  destroy(): void {
    for (const entry of this._entries.values()) {
      if (entry.flashTimer !== null) clearTimeout(entry.flashTimer);
    }
    this._entries.clear();
    this._overlay?.remove();
    this._overlay = null;
  }

  private _positionBox(el: HTMLElement, userCols: number, userRows: number): void {
    if (this._ownCols === 0 || this._ownRows === 0) return;
    const pctW = (userCols / this._ownCols) * 100;
    const pctH = (userRows / this._ownRows) * 100;
    // Clamp to [5%, 200%] so tiny or huge windows don't look broken
    el.style.width = `${Math.min(pctW, 200)}%`;
    el.style.height = `${Math.min(pctH, 200)}%`;
  }
}
