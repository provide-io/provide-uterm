//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * ProvideHijack - Embeddable terminal hijack control widget.
 *
 * Connects to the TermHub browser WebSocket endpoint (/ws/browser/{workerId}/term)
 * and provides live terminal viewing plus hijack controls (pause/step/release).
 *
 * Usage:
 *   const w = new ProvideHijack(containerEl, { workerId: 'myworker' });
 *   w.connect();    // called automatically on construction
 *   w.disconnect(); // close WS
 *   w.dispose();    // tear down entirely
 */

import type { HijackConfig, XTerminal } from "./hijack-codec.js";
import { escapeHijackHtml } from "./hijack-ui.js";

import "./session-element.js";
import type { UtermSessionElement } from "./session-element.js";

// ── Module-level guards ───────────────────────────────────────────────────────
let _hijackInstanceCount = 0;

// ── ProvideHijack class ─────────────────────────────────────────────────────────
export class ProvideHijack {
  private readonly _container: HTMLElement;
  private readonly _sessionElement: UtermSessionElement;
  private readonly _uid: number;

  /**
   * Create an embeddable hijack control widget.
   *
   * @param container - Element to mount the widget into.
   * @param config - Configuration options.
   */
  constructor(container: HTMLElement, config: Partial<HijackConfig> = {}) {
    this._container = container;
    this._uid = ++_hijackInstanceCount;

    this._sessionElement = document.createElement("uterm-session") as UtermSessionElement;
    this._sessionElement.config = config;
    this._sessionElement.uid = this._uid;

    this._container.appendChild(this._sessionElement);

    // Expose widget on the element for programmatic access (demo recording, tests).
    // biome-ignore lint/suspicious/noExplicitAny: bridge for external callers
    (this._sessionElement as any).__provideHijack = this;

    this.connect();
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  /** Open the WebSocket connection. Called automatically on construction. */
  connect(): void {
    this._sessionElement.connect();
  }

  /** Close the WebSocket connection. */
  disconnect(): void {
    this._sessionElement.disconnect();
  }

  /** Tear down entirely: xterm, WebSocket, ResizeObserver, and DOM. */
  dispose(): void {
    this._sessionElement.dispose();
    if (this._sessionElement.parentNode) {
      this._sessionElement.parentNode.removeChild(this._sessionElement);
    }
  }

  /** Send a control message over the WebSocket (e.g. presence_update, control_request). */
  sendControlMessage(msg: Record<string, unknown>): void {
    this._sessionElement.sendControlMessage(msg);
  }

  /** The terminal wrapper element, available immediately after construction. */
  get terminalElement(): HTMLElement | null {
    return this._sessionElement.terminalElement;
  }

  /** The xterm.js Terminal instance, or null if not yet initialized. */
  get terminal(): XTerminal | null {
    return this._sessionElement.terminal;
  }
}

// ── Global exposure for CDN / script-tag use ──────────────────────────────────
// biome-ignore lint/suspicious/noExplicitAny: window global access
if (typeof window !== "undefined") (window as any).ProvideHijack = ProvideHijack;

// Re-exported so external code that imported the helper from hijack.ts directly
// still works after the WS-lifecycle extraction.
export { escapeHijackHtml };
