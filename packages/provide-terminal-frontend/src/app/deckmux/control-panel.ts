//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { ContextAction, DeckMuxUser } from "./types.js";

const TOAST_AUTO_DISMISS_MS = 6_000;
const KEYSTROKE_AUTO_HIDE_MS = 2_000;

export class DeckMuxControlPanel {
  private readonly _container: HTMLElement;
  private _contextMenu: HTMLElement | null = null;
  private _toastContainer: HTMLElement | null = null;
  private _countdownTimer: ReturnType<typeof setInterval> | null = null;
  private _toastDismissTimer: ReturnType<typeof setTimeout> | null = null;
  private _keystrokeEls = new Map<string, { el: HTMLElement; timer: ReturnType<typeof setTimeout> | null }>();
  private _menuCloseHandler: ((e: MouseEvent) => void) | null = null;

  constructor(container: HTMLElement) {
    this._container = container;
    this._buildToastContainer();
  }

  private _buildToastContainer(): void {
    const tc = document.createElement("div");
    tc.className = "dm-toast-container";
    this._toastContainer = tc;
    this._container.appendChild(tc);
  }

  showContextMenu(
    _userId: string,
    user: DeckMuxUser,
    position: { x: number; y: number },
    actions: ContextAction[],
  ): void {
    this.hideContextMenu();

    const menu = document.createElement("div");
    menu.className = "dm-context-menu";
    menu.style.left = `${position.x}px`;
    menu.style.top = `${position.y}px`;

    const header = document.createElement("div");
    header.className = "dm-context-menu-header";

    const dot = document.createElement("span");
    dot.className = "dm-context-menu-dot";
    dot.style.background = user.color;

    const userName = document.createElement("span");
    userName.textContent = user.name;

    header.appendChild(dot);
    header.appendChild(userName);
    menu.appendChild(header);

    for (const action of actions) {
      const item = document.createElement("button");
      item.className = "dm-context-menu-item";
      if (action.danger) item.classList.add("dm-context-menu-item--danger");

      const iconEl = document.createElement("span");
      iconEl.className = "dm-context-menu-icon";
      iconEl.textContent = action.icon;

      const labelWrap = document.createElement("span");
      labelWrap.className = "dm-context-menu-label-wrap";

      const labelEl = document.createElement("span");
      labelEl.className = "dm-context-menu-label";
      labelEl.textContent = action.label;
      labelWrap.appendChild(labelEl);

      if (action.sublabel) {
        const sub = document.createElement("span");
        sub.className = "dm-context-menu-sublabel";
        sub.textContent = action.sublabel;
        labelWrap.appendChild(sub);
      }

      item.appendChild(iconEl);
      item.appendChild(labelWrap);
      item.addEventListener("click", () => {
        this.hideContextMenu();
        action.onClick();
      });
      menu.appendChild(item);
    }

    this._contextMenu = menu;
    this._container.appendChild(menu);

    // Adjust if menu overflows viewport
    requestAnimationFrame(() => {
      if (!menu.isConnected) return;
      const rect = menu.getBoundingClientRect();
      if (rect.right > window.innerWidth) {
        menu.style.left = `${position.x - rect.width}px`;
      }
      if (rect.bottom > window.innerHeight) {
        menu.style.top = `${position.y - rect.height}px`;
      }
    });

    const closeHandler = (e: MouseEvent) => {
      if (!menu.contains(e.target as Node)) {
        this.hideContextMenu();
      }
    };
    this._menuCloseHandler = closeHandler;
    setTimeout(() => document.addEventListener("click", closeHandler), 0);
  }

  hideContextMenu(): void {
    if (this._menuCloseHandler) {
      document.removeEventListener("click", this._menuCloseHandler);
      this._menuCloseHandler = null;
    }
    this._contextMenu?.remove();
    this._contextMenu = null;
  }

  showRequestToast(fromName: string, fromColor: string, onAccept: () => void, onDeny: () => void): void {
    this._clearToastTimer();
    const toast = this._buildToast("dm-toast--request");

    const dot = document.createElement("span");
    dot.className = "dm-toast-dot";
    dot.style.background = fromColor;

    const msg = document.createElement("span");
    msg.className = "dm-toast-message";
    msg.textContent = `${fromName} wants control`;

    const actions = document.createElement("div");
    actions.className = "dm-toast-actions";

    const acceptBtn = document.createElement("button");
    acceptBtn.className = "dm-toast-btn dm-toast-btn--accept";
    acceptBtn.textContent = "Accept";
    acceptBtn.addEventListener("click", () => {
      this.hideToasts();
      onAccept();
    });

    const denyBtn = document.createElement("button");
    denyBtn.className = "dm-toast-btn dm-toast-btn--deny";
    denyBtn.textContent = "Deny";
    denyBtn.addEventListener("click", () => {
      this.hideToasts();
      onDeny();
    });

    actions.appendChild(acceptBtn);
    actions.appendChild(denyBtn);
    toast.appendChild(dot);
    toast.appendChild(msg);
    toast.appendChild(actions);

    this._toastContainer?.appendChild(toast);
    this._toastDismissTimer = setTimeout(() => this.hideToasts(), TOAST_AUTO_DISMISS_MS);
  }

  showTransferToast(toName: string, toColor: string): void {
    this._clearToastTimer();
    const toast = this._buildToast("dm-toast--transfer");

    const dot = document.createElement("span");
    dot.className = "dm-toast-dot";
    dot.style.background = toColor;

    const msg = document.createElement("span");
    msg.className = "dm-toast-message";
    msg.textContent = `Control transferred to ${toName}`;

    toast.appendChild(dot);
    toast.appendChild(msg);
    this._toastContainer?.appendChild(toast);
    this._toastDismissTimer = setTimeout(() => this.hideToasts(), TOAST_AUTO_DISMISS_MS);
  }

  showAutoTransferWarning(toName: string, secondsRemaining: number): void {
    this._clearToastTimer();
    this._stopCountdown();

    const toast = this._buildToast("dm-toast--warning");

    const msg = document.createElement("span");
    msg.className = "dm-toast-message";
    msg.textContent = `Auto-transferring control to ${toName} in ${secondsRemaining}s`;

    const countdownBar = document.createElement("div");
    countdownBar.className = "dm-countdown-bar";
    const fill = document.createElement("div");
    fill.className = "dm-countdown-fill";
    fill.style.width = "100%";
    countdownBar.appendChild(fill);

    toast.appendChild(msg);
    toast.appendChild(countdownBar);
    this._toastContainer?.appendChild(toast);

    let remaining = secondsRemaining;
    this._countdownTimer = setInterval(() => {
      remaining -= 0.1;
      const pct = Math.max(0, (remaining / secondsRemaining) * 100);
      fill.style.width = `${pct}%`;
      if (remaining <= 0) this._stopCountdown();
    }, 100);
  }

  showAutoTransferComplete(toName: string): void {
    this._stopCountdown();
    this.hideToasts();
    this.showTransferToast(toName, "#6b7280");
  }

  hideToasts(): void {
    this._clearToastTimer();
    this._stopCountdown();
    if (this._toastContainer) {
      this._toastContainer.innerHTML = "";
    }
  }

  showKeystrokeQueue(userId: string, displayKeys: string, position: { x: number; y: number }): void {
    const existing = this._keystrokeEls.get(userId);
    if (existing) {
      if (existing.timer !== null) clearTimeout(existing.timer);
      existing.el.textContent = displayKeys;
      existing.el.style.left = `${position.x}px`;
      existing.el.style.top = `${position.y}px`;
      existing.timer = setTimeout(() => this.hideKeystrokeQueue(userId), KEYSTROKE_AUTO_HIDE_MS);
    } else {
      const el = document.createElement("div");
      el.className = "dm-keystroke-queue";
      el.textContent = displayKeys;
      el.style.left = `${position.x}px`;
      el.style.top = `${position.y}px`;
      el.dataset.userId = userId;
      this._container.appendChild(el);
      const timer = setTimeout(() => this.hideKeystrokeQueue(userId), KEYSTROKE_AUTO_HIDE_MS);
      this._keystrokeEls.set(userId, { el, timer });
    }
  }

  hideKeystrokeQueue(userId: string): void {
    const entry = this._keystrokeEls.get(userId);
    if (!entry) return;
    if (entry.timer !== null) clearTimeout(entry.timer);
    entry.el.remove();
    this._keystrokeEls.delete(userId);
  }

  destroy(): void {
    this.hideContextMenu();
    this.hideToasts();
    for (const [userId] of this._keystrokeEls) {
      this.hideKeystrokeQueue(userId);
    }
    this._toastContainer?.remove();
    this._toastContainer = null;
  }

  private _buildToast(extraClass: string): HTMLElement {
    const toast = document.createElement("div");
    toast.className = `dm-toast ${extraClass}`;
    return toast;
  }

  private _clearToastTimer(): void {
    if (this._toastDismissTimer !== null) {
      clearTimeout(this._toastDismissTimer);
      this._toastDismissTimer = null;
    }
  }

  private _stopCountdown(): void {
    if (this._countdownTimer !== null) {
      clearInterval(this._countdownTimer);
      this._countdownTimer = null;
    }
  }
}
