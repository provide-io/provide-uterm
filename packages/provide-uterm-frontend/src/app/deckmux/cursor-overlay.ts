//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import "./cursor-overlay-element.js";
import type { CursorOverlayElement, OverlayUser } from "./cursor-overlay-element.js";

export class DeckMuxCursorOverlay {
  private readonly _terminalContainer: HTMLElement;
  private _el: CursorOverlayElement | null = null;
  private _users = new Map<string, OverlayUser>();
  private _ownerId: string | null = null;

  constructor(terminalContainer: HTMLElement) {
    this._terminalContainer = terminalContainer;
    this._buildOverlay();
  }

  private _buildOverlay(): void {
    const el = document.createElement("uterm-cursor-overlay") as CursorOverlayElement;
    this._el = el;
    this._terminalContainer.appendChild(el);
  }

  setPin(userId: string, line: number, name: string, color: string, isOwner: boolean): void {
    const user = this._users.get(userId) || { userId, name, color };
    user.name = name;
    user.color = color;
    user.pin = { line };
    this._users.set(userId, user);
    if (isOwner) {
      this._ownerId = userId;
    } else if (this._ownerId === userId) {
      this._ownerId = null;
    }
    this._sync();
  }

  setOwner(userId: string | null): void {
    this._ownerId = userId;
    this._sync();
  }

  removePin(userId: string): void {
    const user = this._users.get(userId);
    if (user) {
      delete user.pin;
      this._cleanupUser(userId, user);
      this._sync();
    }
  }

  setSelection(userId: string, startLine: number, endLine: number, color: string): void {
    const user = this._users.get(userId) || { userId, name: "", color };
    user.color = color;
    user.selection = { startLine, endLine };
    this._users.set(userId, user);
    this._sync();
  }

  removeSelection(userId: string): void {
    const user = this._users.get(userId);
    if (user) {
      delete user.selection;
      this._cleanupUser(userId, user);
      this._sync();
    }
  }

  setVisible(visible: boolean): void {
    if (this._el) {
      this._el.visible = visible;
    }
  }

  destroy(): void {
    this._el?.remove();
    this._el = null;
    this._users.clear();
  }

  private _cleanupUser(userId: string, user: OverlayUser): void {
    if (!user.pin && !user.selection) {
      this._users.delete(userId);
      if (this._ownerId === userId) {
        this._ownerId = null;
      }
    }
  }

  private _sync(): void {
    if (!this._el) return;
    this._el.users = Array.from(this._users.values());
    this._el.ownerId = this._ownerId;
  }
}
