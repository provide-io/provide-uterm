//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

interface UserRange {
  color: string;
  range: { top: number; height: number };
  options: {
    isOwner?: boolean;
    selection?: { top: number; height: number };
    pin?: number;
    name?: string;
    idle?: boolean;
  };
}

const MAX_USERS = 7;
const BAR_WIDTH = 4; // px — bar width
const BAR_GAP = 1; // px — gap between bars
const SLOT_STEP = BAR_WIDTH + BAR_GAP; // 5px per slot

export class DeckMuxEdgeIndicators {
  private readonly _terminalContainer: HTMLElement;
  private _track: HTMLElement | null = null;
  private _namesVisible = false;
  private _slots: (string | null)[] = Array(MAX_USERS).fill(null) as (string | null)[];
  private _users = new Map<
    string,
    { state: UserRange; slot: number; barEl: HTMLElement; nameEl: HTMLElement | null }
  >();

  constructor(terminalContainer: HTMLElement) {
    this._terminalContainer = terminalContainer;
    this._buildTrack();
  }

  private _buildTrack(): void {
    const track = document.createElement("div");
    track.className = "dm-edge-track";
    this._track = track;
    this._terminalContainer.appendChild(track);
  }

  private _assignSlot(userId: string): number {
    const existing = this._slots.indexOf(userId);
    if (existing !== -1) return existing;
    const free = this._slots.indexOf(null);
    if (free !== -1) {
      this._slots[free] = userId;
      return free;
    }
    return -1; // at capacity
  }

  private _freeSlot(userId: string): void {
    const idx = this._slots.indexOf(userId);
    if (idx !== -1) this._slots[idx] = null;
  }

  setUser(
    userId: string,
    color: string,
    range: { top: number; height: number },
    options: {
      isOwner?: boolean;
      selection?: { top: number; height: number };
      pin?: number;
      name?: string;
      idle?: boolean;
    } = {},
  ): void {
    const existing = this._users.get(userId);
    const state: UserRange = { color, range, options };

    if (existing) {
      existing.state = state;
      this._syncBar(existing.slot, existing.barEl, existing.nameEl, state);
    } else {
      const slot = this._assignSlot(userId);
      if (slot === -1) return; // over capacity

      const barEl = document.createElement("div");
      barEl.className = "dm-edge-bar";
      barEl.dataset.userId = userId;
      barEl.style.left = `${slot * SLOT_STEP}px`;

      let nameEl: HTMLElement | null = null;
      if (options.name) {
        nameEl = document.createElement("span");
        nameEl.className = "dm-edge-name";
        nameEl.textContent = options.name;
        nameEl.style.display = this._namesVisible ? "" : "none";
        barEl.appendChild(nameEl);
      }

      this._track?.appendChild(barEl);
      this._users.set(userId, { state, slot, barEl, nameEl });
      this._syncBar(slot, barEl, nameEl, state);
    }
  }

  removeUser(userId: string): void {
    const entry = this._users.get(userId);
    if (!entry) return;
    entry.barEl.remove();
    this._freeSlot(userId);
    this._users.delete(userId);
  }

  setNamesVisible(visible: boolean): void {
    this._namesVisible = visible;
    for (const entry of this._users.values()) {
      if (entry.nameEl) {
        entry.nameEl.style.display = visible ? "" : "none";
      }
    }
  }

  destroy(): void {
    this._track?.remove();
    this._track = null;
    this._slots.fill(null);
    this._users.clear();
  }

  private _syncBar(_slot: number, barEl: HTMLElement, nameEl: HTMLElement | null, state: UserRange): void {
    const { color, range, options } = state;
    const isOwner = options.isOwner ?? false;

    barEl.style.top = `${range.top * 100}%`;
    barEl.style.height = `${range.height * 100}%`;
    barEl.style.setProperty("--dm-user-color", color);
    barEl.classList.toggle("dm-edge-bar--owner", isOwner);
    barEl.classList.toggle("dm-edge-bar--idle", options.idle ?? false);

    // Remove old selection/pin children, keep nameEl
    const toRemove: Element[] = [];
    for (const child of barEl.children) {
      if (child !== nameEl) toRemove.push(child);
    }
    for (const child of toRemove) child.remove();

    if (options.selection) {
      const sel = document.createElement("div");
      sel.className = "dm-edge-selection";
      sel.style.top = `${((options.selection.top - range.top) / range.height) * 100}%`;
      sel.style.height = `${(options.selection.height / range.height) * 100}%`;
      barEl.appendChild(sel);
    }

    if (options.pin !== undefined) {
      const pin = document.createElement("div");
      pin.className = "dm-edge-pin";
      const pinOffset = (options.pin - range.top) / range.height;
      pin.style.top = `${pinOffset * 100}%`;
      barEl.appendChild(pin);
    }

    if (nameEl) {
      nameEl.textContent = options.name ?? "";
      nameEl.style.display = this._namesVisible ? "" : "none";
      // Re-append nameEl so it stays on top
      barEl.appendChild(nameEl);
    }
  }
}
