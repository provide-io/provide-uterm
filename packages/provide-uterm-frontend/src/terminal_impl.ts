//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * ProvideTerminal — embeddable xterm.js widget with persistent settings,
 * theme picker, and auto-reconnecting WebSocket. The settings model and theme
 * palette live in sibling modules; this file owns the WS
 * lifecycle, and the public class registered on window.
 * The DOM and xterm initialization have been refactored into the <uterm-terminal> Lit element.
 */

import { ControlChannelDecoder, encodeWsFrame } from "./hijack-codec.js";
import type { TerminalConfig } from "./terminal-settings.js";
import "./terminal-element.js";
import type { TerminalElement } from "./terminal-element.js";

// Throttle for outbound consumption ACKs: at most one ack per window, carrying
// the cumulative bytes received so the DO can size its Tier-A backpressure
// window (see docs/ard-cloudflare-backpressure.md).
const ACK_THROTTLE_MS = 100;

declare global {
  interface Window {
    ProvideTerminal?: typeof ProvideTerminal;
    demoTerminal?: ProvideTerminal;
  }
}

export class ProvideTerminal {
  private readonly container: HTMLElement;
  private readonly config: TerminalConfig;
  private ws: WebSocket | null = null;
  private connected = false;
  private reconnectEnabled = false;
  private reconnectTimer: number | null = null;
  private readonly wsDecoder = new ControlChannelDecoder();
  
  // Tier-A backpressure: cumulative bytes received on this connection, ACKed to
  // the DO on a throttle so it can pause the producer when the browser lags.
  private _ackBytes = 0;
  private _ackTimer: number | null = null;
  
  private litElement: TerminalElement;

  constructor(container: HTMLElement, config: TerminalConfig = {}) {
    if (typeof window !== "undefined") {
      if (window.Terminal === undefined) {
        throw new Error("xterm.js (Terminal) not loaded — include @xterm/xterm before terminal.js");
      }
      if (window.FitAddon === undefined) {
        throw new Error("xterm addon-fit (FitAddon) not loaded — include @xterm/addon-fit before terminal.js");
      }
    }

    this.container = container;
    this.config = config;
    
    this.litElement = document.createElement("uterm-terminal") as TerminalElement;
    this.litElement.config = this.config;
    this.container.appendChild(this.litElement);
    
    this.litElement.addEventListener("uterm-data", (e: Event) => {
      const data = (e as CustomEvent<string>).detail;
      this.handleTerminalInput(data);
    });

    this.connect();
  }

  connect(): void {
    this.connectWebSocket();
  }

  disconnect(): void {
    this.reconnectEnabled = false;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.clearAckTimer();
  }

  getBufferText(maxLines = 200): string {
    return this.litElement.getBufferText(maxLines);
  }

  focus(): void {
    this.litElement.focusTerminal();
  }

  dispose(): void {
    this.disconnect();
    this.litElement.dispose();
    if (this.litElement.parentNode) {
      this.litElement.parentNode.removeChild(this.litElement);
    }
  }

  private resolveWsUrl(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    if (this.config.wsUrl) {
      return this.config.wsUrl.startsWith("/") ? `${proto}//${location.host}${this.config.wsUrl}` : this.config.wsUrl;
    }
    return `${proto}//${location.host}/ws/terminal`;
  }

  private handleTerminalInput(data: string): void {
    if (!data || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(data);
  }

  /** Schedule a single throttled consumption ACK (Tier-A backpressure). */
  private scheduleAck(): void {
    if (this._ackTimer !== null) return;
    this._ackTimer = window.setTimeout(() => {
      this._ackTimer = null;
      this.sendAck();
    }, ACK_THROTTLE_MS);
  }

  private sendAck(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(encodeWsFrame({ type: "ack", bytes: this._ackBytes }));
    }
  }

  private clearAckTimer(): void {
    if (this._ackTimer !== null) {
      window.clearTimeout(this._ackTimer);
      this._ackTimer = null;
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      if (this.reconnectEnabled && this.ws === null) {
        this.connectWebSocket();
      }
    }, 1000);
  }

  private connectWebSocket(): void {
    this.reconnectEnabled = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    const ws = new WebSocket(this.resolveWsUrl());
    this.ws = ws;
    this.wsDecoder.reset();
    // New connection → the DO's per-socket sent counter restarts at 0.
    this._ackBytes = 0;
    this.clearAckTimer();
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      this.litElement.connected = true;
      this.litElement.updateStatus(true);
    };
    ws.onmessage = (event) => {
      const payload = typeof event.data === "string" ? event.data : "";
      if (!payload) return;
      // Count raw received bytes (pre-decode, matching the DO's per-frame
      // msg_len) and ACK on a throttle to drive Tier-A backpressure.
      this._ackBytes += payload.length;
      this.scheduleAck();
      // Decode the inline control-channel framing so the user never sees raw
      // JSON control frames mixed into terminal output. Control chunks are
      // discarded here — ProvideTerminal has no hijack UI to surface them.
      let frames: ReturnType<ControlChannelDecoder["feed"]>;
      try {
        frames = this.wsDecoder.feed(payload);
      } catch {
        // Stream is corrupt; reset decoder and fall back to writing the raw
        // payload so users don't get stuck on a blank screen.
        this.wsDecoder.reset();
        this.litElement.writeData(payload);
        return;
      }
      for (const frame of frames) {
        if (frame.type === "data") this.litElement.writeData(frame.data);
      }
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.connected = false;
      this.litElement.connected = false;
      this.litElement.updateStatus(false);
      if (this.reconnectEnabled) this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }
}

if (typeof window !== "undefined") {
  window.ProvideTerminal = ProvideTerminal;
}
