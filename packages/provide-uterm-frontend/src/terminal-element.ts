//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { LitElement, type PropertyValues, html } from "lit";
import { property } from "lit/decorators.js";
import { buildSettingsPanelHtml, DEFAULTS, loadSettings, saveSettings, type TerminalConfig, type TerminalSettings } from "./terminal-settings.js";
import { applyColors, applyThemeClasses, asThemeName, THEME_DEFAULTS, type ThemeName } from "./terminal-themes.js";
import { ControlChannelDecoder, encodeWsFrame } from "./hijack-codec.js";

const ACK_THROTTLE_MS = 100;
const TEXT_ENCODER = new TextEncoder();

function utf8ByteLength(value: string): number {
  return TEXT_ENCODER.encode(value).byteLength;
}

interface XtermLine {
  translateToString(trimRight?: boolean): string;
}

interface XtermBuffer {
  active: {
    baseY: number;
    cursorY: number;
    getLine(index: number): XtermLine | undefined;
  };
}

export interface XtermTerminal {
  options: {
    fontSize: number;
    theme?: Record<string, unknown>;
  };
  buffer: XtermBuffer;
  write(data: string): void;
  open(element: HTMLElement): void;
  focus(): void;
  dispose(): void;
  onData(callback: (data: string) => void): void;
  attachCustomKeyEventHandler(callback: (event: KeyboardEvent) => boolean): void;
  loadAddon(addon: unknown): void;
}

interface XtermCtor {
  new (config: Record<string, unknown>): XtermTerminal;
}

export interface FitAddonInstance {
  fit(): void;
  proposeDimensions(): { cols: number } | undefined;
}

interface FitAddonCtor {
  new (): FitAddonInstance;
}

interface FitAddonGlobal {
  FitAddon: FitAddonCtor;
}

declare global {
  interface Window {
    Terminal?: XtermCtor;
    FitAddon?: FitAddonGlobal;
  }
}

let instanceCount = 0;

export class TerminalElement extends LitElement {
  @property({ type: Object }) config: TerminalConfig = {};
  @property({ type: Boolean }) connected = false;

  private uid = ++instanceCount;
  private term: XtermTerminal | null = null;
  private fitAddon: FitAddonInstance | null = null;
  private settings: TerminalSettings = { ...DEFAULTS };
  private resizeObserver: ResizeObserver | null = null;
  private _firstDataWritten = false;
  /** Bytes received before xterm was constructed (WS can open in connect() before firstUpdated). */
  private _pendingWrites: string[] = [];

  private ws: WebSocket | null = null;
  private reconnectEnabled = false;
  private reconnectTimer: number | null = null;
  private readonly wsDecoder = new ControlChannelDecoder();
  private _ackBytes = 0;
  private _ackTimer: number | null = null;

  override createRenderRoot() {
    return this; // Render to light DOM to avoid shadow boundary issues with xterm
  }

  override connectedCallback() {
    super.connectedCallback();
    this.className = "provide-uterm";
    this.settings = loadSettings(this.config);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    this.dispose();
  }

  override firstUpdated() {
    this.bindSettingsEvents();
    this.createTerminal();
    document.fonts.ready.then(() => requestAnimationFrame(() => this.fitWithMinCols(this.settings.cols)));
  }

  override updated(changedProperties: PropertyValues) {
    if (changedProperties.has('connected')) {
      this.updateStatus(this.connected);
    }
  }

  private q<T extends Element>(id: string): T {
    const node = this.querySelector(`#${id}-${this.uid}`);
    if (!(node instanceof Element)) {
      throw new Error(`Missing terminal element: ${id}`);
    }
    return node as T;
  }



  private buildFrame(): import("lit").TemplateResult {
    const title = (this.config.title || "Warp Agent Runtime Platform").toUpperCase();
    const label = this.config.title || "Warp Agent Runtime Platform";
    return html`
      <div class="terminal-frame">
        <div class="frame-header">
          <span class="frame-header-title">${title}</span>
          <div class="frame-status">
            <span class="status-dot" data-status-dot="1" role="status" aria-label="Connecting"></span>
            <span data-status-text="1">Connecting...</span>
          </div>
        </div>
        <div class="frame-titlebar">${title}</div>
        <div class="screen-inset">
          <div class="terminal-div" id="terminalDiv-${this.uid}"></div>
        </div>
        <div class="frame-statusbar">
          <span>ANSI Terminal</span>
          <div class="frame-statusbar-right">
            <div class="frame-status">
              <span class="status-dot" data-status-dot="1" role="status" aria-label="Connecting"></span>
              <span data-status-text="1">Connecting...</span>
            </div>
            <span data-connection-info="1">80×25</span>
          </div>
        </div>
        <div class="frame-bottom">
          <span class="frame-label">${label}</span>
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="frame-status">
              <span class="status-dot" data-status-dot="1" role="status" aria-label="Connecting"></span>
              <span data-status-text="1">Connecting...</span>
            </div>
            <div class="led" data-led-indicator="1"></div>
          </div>
        </div>
      </div>`;
  }

  override render() {
    return html`
      ${buildSettingsPanelHtml(this.uid)}
      <div class="page-wrapper" id="pageWrapper-${this.uid}">
        <div class="frame-root" id="frameRoot-${this.uid}">
          ${this.buildFrame()}
        </div>
        <div class="loading" id="loadingScreen-${this.uid}">
          <div>
            <div class="loading-spinner"></div>
            Initializing Terminal Connection...
          </div>
        </div>
      </div>
    `;
  }

  private applySettingsToUi(): void {
    this.querySelectorAll(".theme-btn").forEach((node) => {
      if (node instanceof HTMLElement) {
        node.classList.toggle("active", node.dataset.theme === this.settings.theme);
      }
    });
    this.q<HTMLInputElement>("setCols").value = String(this.settings.cols);
    this.q<HTMLElement>("valCols").textContent = String(this.settings.cols);
    this.q<HTMLInputElement>("setRows").value = String(this.settings.rows);
    this.q<HTMLElement>("valRows").textContent = String(this.settings.rows);
    this.q<HTMLInputElement>("setFontSize").value = String(this.settings.fontSize);
    this.q<HTMLElement>("valFontSize").textContent = `${this.settings.fontSize}px`;
    this.q<HTMLInputElement>("setPageBg").value = this.settings.pageBg;
    this.q<HTMLInputElement>("setTermBg").value = this.settings.termBg;
    this.q<HTMLInputElement>("fxScanlines").checked = this.settings.scanlines;
    this.q<HTMLInputElement>("fxVignette").checked = this.settings.vignette;
    this.q<HTMLInputElement>("fxGlow").checked = this.settings.glow;
  }

  private applyRuntimeSettings(): void {
    applyThemeClasses(this, this.settings.theme, this.settings);
    applyColors(this, this.settings.pageBg, this.settings.termBg);
    this.applySettingsToUi();
    saveSettings(this.settings);
    if (this.term !== null) {
      this.term.options.fontSize = this.settings.fontSize;
      this.term.options.theme = {
        ...(this.term.options.theme || {}),
        background: this.settings.termBg,
        cursorAccent: this.settings.termBg,
      };
    }
    requestAnimationFrame(() => this.fitWithMinCols(this.settings.cols));
  }

  private bindSettingsEvents(): void {
    const overlay = this.q<HTMLElement>("settingsOverlay");
    const panel = this.q<HTMLElement>("settingsPanel");
    const gear = this.q<HTMLButtonElement>("gearBtn");
    const togglePanel = (open: boolean): void => {
      panel.classList.toggle("open", open);
      overlay.classList.toggle("open", open);
    };
    gear.addEventListener("click", () => togglePanel(!panel.classList.contains("open")));
    overlay.addEventListener("click", () => togglePanel(false));

    this.querySelectorAll(".theme-btn").forEach((node) => {
      if (!(node instanceof HTMLButtonElement)) return;
      node.addEventListener("click", () => {
        const nextTheme: ThemeName = asThemeName(node.dataset.theme);
        this.settings.theme = nextTheme;
        const themeDefaults = THEME_DEFAULTS[nextTheme];
        this.settings.scanlines = themeDefaults.scanlines;
        this.settings.vignette = themeDefaults.vignette;
        this.settings.glow = themeDefaults.glow;
        this.applyRuntimeSettings();
      });
    });

    const bindRange = (
      id: string,
      outputId: string,
      update: (value: string) => void,
      format: (value: string) => string,
    ): void => {
      const input = this.q<HTMLInputElement>(id);
      const output = this.q<HTMLElement>(outputId);
      input.addEventListener("input", () => {
        update(input.value);
        output.textContent = format(input.value);
        this.applyRuntimeSettings();
      });
    };

    bindRange(
      "setCols",
      "valCols",
      (value) => {
        this.settings.cols = Number(value);
      },
      (value) => value,
    );
    bindRange(
      "setRows",
      "valRows",
      (value) => {
        this.settings.rows = Number(value);
      },
      (value) => value,
    );
    bindRange(
      "setFontSize",
      "valFontSize",
      (value) => {
        this.settings.fontSize = Number(value);
      },
      (value) => `${value}px`,
    );

    const pageBgInput = this.q<HTMLInputElement>("setPageBg");
    pageBgInput.addEventListener("input", () => {
      this.settings.pageBg = pageBgInput.value;
      this.applyRuntimeSettings();
    });

    const termBgInput = this.q<HTMLInputElement>("setTermBg");
    termBgInput.addEventListener("input", () => {
      this.settings.termBg = termBgInput.value;
      this.applyRuntimeSettings();
    });

    const bindCheckbox = (id: string, update: (value: boolean) => void): void => {
      const input = this.q<HTMLInputElement>(id);
      input.addEventListener("input", () => {
        update(input.checked);
        this.applyRuntimeSettings();
      });
    };
    bindCheckbox("fxScanlines", (value) => {
      this.settings.scanlines = value;
    });
    bindCheckbox("fxVignette", (value) => {
      this.settings.vignette = value;
    });
    bindCheckbox("fxGlow", (value) => {
      this.settings.glow = value;
    });

    this.applyRuntimeSettings();
  }

  private fitWithMinCols(minCols: number): void {
    if (this.fitAddon === null || this.term === null) return;
    const proposed = this.fitAddon.proposeDimensions();
    if (!proposed || proposed.cols <= 0) return;
    if (proposed.cols < minCols) {
      this.term.options.fontSize = Math.max(6, Math.floor((this.term.options.fontSize * proposed.cols) / minCols));
    }
    this.fitAddon.fit();
  }

  private createTerminal(): void {
    if (window.Terminal === undefined) {
      throw new Error("xterm.js (Terminal) not loaded — include @xterm/xterm before terminal.js");
    }
    if (window.FitAddon === undefined) {
      throw new Error("xterm addon-fit (FitAddon) not loaded — include @xterm/addon-fit before terminal.js");
    }

    applyThemeClasses(this, this.settings.theme, this.settings);
    applyColors(this, this.settings.pageBg, this.settings.termBg);

    this.term = new window.Terminal({
      theme: {
        background: this.settings.termBg,
        foreground: "#e2e8f0",
        cursor: "#22c55e",
        cursorAccent: this.settings.termBg,
        selection: "rgba(34, 197, 94, 0.15)",
        black: "#000000",
      },
      fontFamily: "'Fira Code', 'DejaVu Sans Mono', 'Consolas', monospace",
      fontSize: this.settings.fontSize,
      fontWeight: "normal",
      letterSpacing: 0,
      lineHeight: 1.2,
      allowTransparency: true,
    });

    const terminalDiv = this.q<HTMLElement>("terminalDiv");
    this.term.open(terminalDiv);
    this.fitAddon = new window.FitAddon.FitAddon();
    this.term.loadAddon(this.fitAddon);
    requestAnimationFrame(() => this.fitWithMinCols(this.settings.cols));

    this.resizeObserver?.disconnect();
    this.resizeObserver = new ResizeObserver(() => this.fitWithMinCols(this.settings.cols));
    this.resizeObserver.observe(terminalDiv);

    this.term.focus();
    this.term.onData((data) => this.handleTerminalInput(data));

    // Flush any WS frames that arrived while xterm was not yet ready. Do not
    // re-show the loading overlay if we already painted (or buffered) data.
    if (this._pendingWrites.length > 0 || this._firstDataWritten) {
      const pending = this._pendingWrites;
      this._pendingWrites = [];
      for (const chunk of pending) {
        this.term.write(chunk);
      }
      if (pending.length > 0 || this._firstDataWritten) {
        this._firstDataWritten = true;
        const loading = this.q<HTMLElement>("loadingScreen");
        loading.style.display = "none";
      }
    } else {
      this._firstDataWritten = false;
      const loading = this.q<HTMLElement>("loadingScreen");
      loading.style.removeProperty("display");
    }

    this.updateStatus(this.connected);
  }

  public updateStatus(connected: boolean): void {
    const statusText = connected ? "Connected" : "Disconnected";
    this.querySelectorAll<HTMLElement>("[data-status-dot='1']").forEach((dot) => {
      dot.className = `status-dot${connected ? " connected" : ""}`;
      dot.setAttribute("aria-label", statusText);
    });
    this.querySelectorAll<HTMLElement>("[data-led-indicator='1']").forEach((led) => {
      led.classList.toggle("on", connected);
    });
    this.querySelectorAll<HTMLElement>("[data-status-text='1']").forEach((text) => {
      text.textContent = statusText;
    });
    this.querySelectorAll<HTMLElement>("[data-connection-info='1']").forEach((info) => {
      info.textContent = `${this.settings.cols}×${this.settings.rows}`;
    });
  }

  private handleTerminalInput(data: string): void {
    if (!data || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    // Encode as a control-channel data frame (type=input). Plain text without
    // DLE is wire-identical to encodeDataFrame, but framing keeps mixed
    // streams correct when keys contain DLE.
    this.ws.send(encodeWsFrame({ type: "input", data }));
    this.dispatchEvent(new CustomEvent("uterm-data", { detail: data }));
  }

  private resolveWsUrl(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    if (this.config.wsUrl) {
      return this.config.wsUrl.startsWith("/") ? `${proto}//${location.host}${this.config.wsUrl}` : this.config.wsUrl;
    }
    return `${proto}//${location.host}/ws/terminal`;
  }

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
    this._ackBytes = 0;
    this.clearAckTimer();
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      this.updateStatus(true);
      // Parity with uterm-session / browser role: hub may deliver the current
      // screen only as a control ``snapshot`` (and on connect often already
      // has one). Request one so a quiet shell still paints and clears the
      // loading overlay instead of staying on "Initializing…".
      try {
        ws.send(encodeWsFrame({ type: "snapshot_req" }));
      } catch {
        // Best-effort; reconnect path will retry.
      }
    };
    ws.onmessage = (event) => {
      const payload = typeof event.data === "string" ? event.data : "";
      if (!payload) return;
      this._ackBytes += utf8ByteLength(payload);
      this.scheduleAck();
      let frames: ReturnType<ControlChannelDecoder["feed"]>;
      try {
        frames = this.wsDecoder.feed(payload);
      } catch {
        this.wsDecoder.reset();
        this.writeData(payload);
        return;
      }
      for (const frame of frames) {
        // Data chunks are the common path for live term fan-out (server encodes
        // type=term as terminal data). Control frames carry hello/hijack_state
        // and also snapshot (and any type=term still sent as control JSON).
        if (frame.type === "data") {
          this.writeData(frame.data);
          continue;
        }
        const msg = frame.control;
        const mtype = typeof msg.type === "string" ? msg.type : "";
        if (mtype === "term") {
          const data = typeof msg.data === "string" ? msg.data : "";
          if (data) this.writeData(data);
        } else if (mtype === "snapshot") {
          const screen = typeof msg.screen === "string" ? msg.screen : "";
          // Soft reset + home, then paint screen (same as session-element).
          this.writeData("\x1b[!p\x1b[2J\x1b[H");
          this.writeData(screen.replace(/\n/g, "\r\n"));
        }
        // hello / hijack_state / presence / etc. — intentionally ignored here;
        // this widget is terminal-only (no chrome).
      }
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.connected = false;
      this.updateStatus(false);
      if (this.reconnectEnabled) this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  // --- Public API ---

  public connect(): void {
    this.connectWebSocket();
  }

  public disconnect(): void {
    this.reconnectEnabled = false;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.clearAckTimer();
  }

  public writeData(data: string): void {
    if (!this._firstDataWritten) {
      const loading = this.querySelector(`#loadingScreen-${this.uid}`) as HTMLElement | null;
      if (loading) loading.style.display = "none";
      this._firstDataWritten = true;
    }
    if (this.term === null) {
      // connect() may open the WS before firstUpdated builds xterm; buffer.
      this._pendingWrites.push(data);
      return;
    }
    this.term.write(data);
  }

  public getBufferText(maxLines = 200): string {
    if (!this.term) return "";
    const active = this.term.buffer.active;
    const end = active.baseY + active.cursorY;
    const start = Math.max(0, end - Math.max(1, maxLines) + 1);
    const lines: string[] = [];
    for (let i = start; i <= end; i += 1) {
      const line = active.getLine(i)?.translateToString(true) ?? "";
      if (line.trim()) lines.push(line);
    }
    return lines.join("\n");
  }

  public focusTerminal(): void {
    this.term?.focus();
  }

  public dispose(): void {
    this.disconnect();
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.term?.dispose();
    this.term = null;
    this.fitAddon = null;
    this.remove();
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-terminal": TerminalElement;
  }
}

if (!customElements.get("uterm-terminal")) {
  customElements.define("uterm-terminal", TerminalElement);
}
