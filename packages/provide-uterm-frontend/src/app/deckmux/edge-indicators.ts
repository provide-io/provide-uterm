//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import "./edge-indicators-element.js";
import type { EdgeIndicatorUser } from "./edge-indicators-element.js";

const MAX_USERS = 7;

export class DeckMuxEdgeIndicators {
  private readonly _terminalContainer: HTMLElement;
  private _element: HTMLElement & { users: EdgeIndicatorUser[]; namesVisible: boolean } | null = null;
  private _namesVisible = false;
  private _slots: (string | null)[] = Array(MAX_USERS).fill(null) as (string | null)[];
  private _users = new Map<string, EdgeIndicatorUser>();

  constructor(terminalContainer: HTMLElement) {
    this._terminalContainer = terminalContainer;
    this._buildElement();
  }

  private _buildElement(): void {
    const el = document.createElement("uterm-edge-indicators") as any;
    el.namesVisible = this._namesVisible;
    el.users = [];
    this._element = el;
    this._terminalContainer.appendChild(el);
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

  private _updateElement(): void {
    if (this._element) {
      // Lit needs a new array reference to detect changes if using standard @property({type: Array})
      this._element.users = Array.from(this._users.values());
    }
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
    let user = this._users.get(userId);
    if (!user) {
      const slot = this._assignSlot(userId);
      if (slot === -1) return; // over capacity
      user = { userId, slot, color, range, options };
    } else {
      user.color = color;
      user.range = range;
      user.options = options;
    }
    this._users.set(userId, user);
    this._updateElement();
  }

  removeUser(userId: string): void {
    if (this._users.has(userId)) {
      this._users.delete(userId);
      this._freeSlot(userId);
      this._updateElement();
    }
  }

  setNamesVisible(visible: boolean): void {
    this._namesVisible = visible;
    if (this._element) {
      this._element.namesVisible = visible;
    }
  }

  destroy(): void {
    this._element?.remove();
    this._element = null;
    this._slots.fill(null);
    this._users.clear();
  }
}
