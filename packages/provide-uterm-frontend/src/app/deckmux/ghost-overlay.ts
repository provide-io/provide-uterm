//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import "./ghost-overlay-element.js";
import type { UtermGhostOverlayElement, GhostEntryState } from "./ghost-overlay-element.js";

const FLASH_DURATION_MS = 1800;

interface GhostEntry {
  state: GhostEntryState;
  flashTimer: ReturnType<typeof setTimeout> | null;
}

export class DeckMuxGhostOverlay {
  private readonly _terminalContainer: HTMLElement;
  private _overlayElement: UtermGhostOverlayElement | null = null;
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
    this._overlayElement = document.createElement("uterm-ghost-overlay") as UtermGhostOverlayElement;
    this._terminalContainer.appendChild(this._overlayElement);
    this._updateElement();
  }

  private _updateElement(): void {
    if (!this._overlayElement) return;
    this._overlayElement.visible = this._visible;
    this._overlayElement.ownCols = this._ownCols;
    this._overlayElement.ownRows = this._ownRows;
    this._overlayElement.entries = Array.from(this._entries.values()).map((e) => e.state);
  }

  setOwnDimensions(cols: number, rows: number): void {
    this._ownCols = cols;
    this._ownRows = rows;
    this._updateElement();
  }

  /** Show ghost box for a user (on hover). Stays until hideUser is called. */
  showUser(userId: string, color: string, cols: number, rows: number): void {
    if (!this._visible || cols === 0 || rows === 0) return;
    const existing = this._entries.get(userId);
    if (existing) {
      existing.state.color = color;
      existing.state.cols = cols;
      existing.state.rows = rows;
      existing.state.hidden = false;
    } else {
      this._entries.set(userId, {
        state: { userId, color, cols, rows, hidden: false, flash: false },
        flashTimer: null,
      });
    }
    this._updateElement();
  }

  /** Hide the persistent ghost box for a user (on hover-out). */
  hideUser(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    // Only hide if not in flash mode
    if (entry.flashTimer === null) {
      entry.state.hidden = true;
      this._updateElement();
    }
  }

  /** Briefly flash a ghost box when user resizes. */
  flashUser(userId: string, color: string, cols: number, rows: number): void {
    if (!this._visible || cols === 0 || rows === 0) return;
    this.showUser(userId, color, cols, rows);
    const entry = this._entries.get(userId);
    if (!entry) return;
    if (entry.flashTimer !== null) clearTimeout(entry.flashTimer);
    
    entry.state.flash = true;
    entry.state.hidden = false;
    this._updateElement();

    entry.flashTimer = setTimeout(() => {
      entry.state.flash = false;
      entry.state.hidden = true;
      entry.flashTimer = null;
      this._updateElement();
    }, FLASH_DURATION_MS);
  }

  removeUser(userId: string): void {
    const entry = this._entries.get(userId);
    if (!entry) return;
    if (entry.flashTimer !== null) clearTimeout(entry.flashTimer);
    this._entries.delete(userId);
    this._updateElement();
  }

  setVisible(visible: boolean): void {
    this._visible = visible;
    if (!visible) {
      for (const entry of this._entries.values()) {
        entry.state.hidden = true;
      }
    }
    this._updateElement();
  }

  destroy(): void {
    for (const entry of this._entries.values()) {
      if (entry.flashTimer !== null) clearTimeout(entry.flashTimer);
    }
    this._entries.clear();
    this._overlayElement?.remove();
    this._overlayElement = null;
  }
}
