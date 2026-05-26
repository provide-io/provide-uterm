//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Mutable state shared between ProvideHijack and the free-function WebSocket
 * lifecycle helpers in hijack-websocket.ts. Holds only the WS-relevant fields;
 * DOM references and approval-UI state stay on the ProvideHijack class.
 */

import type { ControlChannelDecoder, ResolvedConfig, XTerminal } from "./hijack-codec.js";

export class HijackState {
  readonly config: ResolvedConfig;
  readonly workerId: string;
  readonly wsDecoder: ControlChannelDecoder;

  ws: WebSocket | null = null;
  term: XTerminal | null = null;

  heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  reconnectAnimTimer: ReturnType<typeof setInterval> | null = null;
  reconnectAttempt = 0;

  wakingTimer: ReturnType<typeof setTimeout> | null = null;
  wakingTimedOut = false;

  hijacked = false;
  hijackedByMe = false;
  canHijack = false;
  workerOnline = false;

  inputMode = "hijack";
  hijackControl = "ws";
  hijackStepSupported = true;
  restHijackId: string | null = null;
  resumeToken: string | null = null;

  /**
   * Server-confirmed role from the `hello` frame, or null until received.
   * Preferred over `config.role` (constructor input) for UX decisions, since
   * the server is authoritative on the actual role the connection holds.
   */
  serverRole: string | null = null;

  /** One-shot guard so the wss+token audit warning only fires once per state. */
  _wssTokenWarned = false;

  constructor(config: ResolvedConfig, workerId: string, wsDecoder: ControlChannelDecoder) {
    this.config = config;
    this.workerId = workerId;
    this.wsDecoder = wsDecoder;
  }
}

/**
 * Callbacks the WS lifecycle helpers invoke back into the owning ProvideHijack
 * instance for UI updates and incoming-message dispatch. Keeps the helpers
 * independent of DOM/HTML element references.
 */
export interface HijackHandlers {
  setStatus(level: string, text: string): void;
  updateStatus(): void;
  updateButtons(): void;
  handleMessage(msg: Record<string, unknown>): void;
}
