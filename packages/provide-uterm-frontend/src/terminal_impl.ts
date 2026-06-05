//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * ProvideTerminal — embeddable xterm.js widget with persistent settings,
 * theme picker, and auto-reconnecting WebSocket. The settings model and theme
 * palette live in sibling modules; this file owns DOM construction, the WS
 * lifecycle, and the public class registered on window.
 */

import { ControlChannelDecoder } from "./hijack-codec.js";
import {
  buildSettingsPanelHtml,
  DEFAULTS,
  loadSettings,
  saveSettings,
  type TerminalConfig,
  type TerminalSettings,
} from "./terminal-settings.js";
import {
  applyColors,
  applyThemeClasses,
  asThemeName,
  THEME_DEFAULTS,
  type ThemeName,
} from "./terminal-themes.js";

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

interface XtermTerminal {
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

interface FitAddonInstance {
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
    ProvideTerminal?: typeof ProvideTerminal;
    demoTerminal?: ProvideTerminal;
  }
}

let cssInjected = false;
let instanceCount = 0;

function resolveCssHref(): string {
  // import.meta.url resolves the module location for ES modules; fall back
  // to a relative path when the host environment does not provide it (some
  // test runners hand back a non-URL string under jsdom).
  try {
    return new URL("./terminal.css", import.meta.url).href;
  } catch {
    return "terminal.css";
  }
}

function injectCss(): void {
  if (cssInjected) return;
  cssInjected = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = resolveCssHref();
  document.head.appendChild(link);
}

export class ProvideTerminal {
  private readonly container: HTMLElement;
  private readonly config: TerminalSettings & { wsUrl?: string };
  private readonly uid: number;
  private term: XtermTerminal | null = null;
  private fitAddon: FitAddonInstance | null = null;
  private ws: WebSocket | null = null;
  private connected = false;
  private reconnectEnabled = false;
  private settings: TerminalSettings = { ...DEFAULTS };
  private root: HTMLElement | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private reconnectTimer: number | null = null;
  private readonly wsDecoder = new ControlChannelDecoder();
  private _firstDataWritten = false;

  constructor(container: HTMLElement, config: TerminalConfig = {}) {
    this.container = container;
    this.config = { ...DEFAULTS, ...config };
    this.uid = ++instanceCount;
    injectCss();
    this.buildDom();
    this.settings = loadSettings(this.config);
    this.bindSettingsEvents();
    this.createTerminal();
    this.connect();
    document.fonts.ready.then(() => requestAnimationFrame(() => this.fitWithMinCols(this.settings.cols)));
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
  }

  getBufferText(maxLines = 200): string {
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

  dispose(): void {
    this.disconnect();
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.term?.dispose();
    this.term = null;
    this.fitAddon = null;
    this.root?.parentNode?.removeChild(this.root);
    this.root = null;
  }

  private q<T extends Element>(id: string): T {
    const node = this.root?.querySelector(`#${id}-${this.uid}`);
    if (!(node instanceof Element)) {
      throw new Error(`Missing terminal element: ${id}`);
    }
    return node as T;
  }

  private buildDom(): void {
    const root = document.createElement("div");
    root.className = "provide-uterm";
    root.innerHTML = `
      ${buildSettingsPanelHtml(this.uid)}
      <div class="page-wrapper" id="pageWrapper-${this.uid}">
        <div class="frame-root" id="frameRoot-${this.uid}"></div>
        <div class="loading" id="loadingScreen-${this.uid}">
          <div>
            <div class="loading-spinner"></div>
            Initializing Terminal Connection...
          </div>
        </div>
      </div>
    `;
    this.root = root;
    this.container.appendChild(root);
  }

  private escapeHtml(value: unknown): string {
    const el = document.createElement("span");
    el.textContent = String(value);
    return el.innerHTML;
  }

  private buildFrame(): string {
    const title = this.escapeHtml((this.config.title || "Warp Agent Runtime Platform").toUpperCase());
    const label = this.escapeHtml(this.config.title || "Warp Agent Runtime Platform");
    return `
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
            <span data-connection-info="1">${this.settings.cols}×${this.settings.rows}</span>
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

  private resolveWsUrl(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    if (this.config.wsUrl) {
      return this.config.wsUrl.startsWith("/") ? `${proto}//${location.host}${this.config.wsUrl}` : this.config.wsUrl;
    }
    return `${proto}//${location.host}/ws/terminal`;
  }

  private updateStatus(connected: boolean): void {
    const statusText = connected ? "Connected" : "Disconnected";
    this.root?.querySelectorAll<HTMLElement>("[data-status-dot='1']").forEach((dot) => {
      dot.className = `status-dot${connected ? " connected" : ""}`;
      dot.setAttribute("aria-label", statusText);
    });
    this.root?.querySelectorAll<HTMLElement>("[data-led-indicator='1']").forEach((led) => {
      led.classList.toggle("on", connected);
    });
    this.root?.querySelectorAll<HTMLElement>("[data-status-text='1']").forEach((text) => {
      text.textContent = statusText;
    });
    this.root?.querySelectorAll<HTMLElement>("[data-connection-info='1']").forEach((info) => {
      info.textContent = `${this.settings.cols}×${this.settings.rows}`;
    });
  }

  private handleTerminalInput(data: string): void {
    if (!data || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(data);
  }

  /** Write data to the xterm instance, hiding the loading screen on first call. */
  private writeData(data: string): void {
    if (!this._firstDataWritten) {
      const loading = this.root?.querySelector(`#loadingScreen-${this.uid}`) as HTMLElement | null;
      if (loading) loading.style.display = "none";
      this._firstDataWritten = true;
    }
    this.term?.write(data);
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
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      this.updateStatus(true);
    };
    ws.onmessage = (event) => {
      const payload = typeof event.data === "string" ? event.data : "";
      if (!payload || this.term === null) return;
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
        this.writeData(payload);
        return;
      }
      for (const frame of frames) {
        if (frame.type === "data") this.writeData(frame.data);
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

  private applySettingsToUi(): void {
    if (this.root === null) return;
    this.root.querySelectorAll(".theme-btn").forEach((node) => {
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
    if (this.root !== null) {
      applyThemeClasses(this.root, this.settings.theme, this.settings);
      applyColors(this.root, this.settings.pageBg, this.settings.termBg);
    }
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

    this.root?.querySelectorAll(".theme-btn").forEach((node) => {
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
    const frameRoot = this.q<HTMLElement>("frameRoot");
    frameRoot.innerHTML = this.buildFrame();
    if (this.root !== null) {
      applyThemeClasses(this.root, this.settings.theme, this.settings);
      applyColors(this.root, this.settings.pageBg, this.settings.termBg);
    }

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

    // Reset the first-data gate so the loading screen is shown again if the
    // terminal is re-created (e.g. on reconnect).
    this._firstDataWritten = false;
    const loading = this.q<HTMLElement>("loadingScreen");
    loading.style.removeProperty("display");

    this.updateStatus(this.connected);
  }
}

if (typeof window !== "undefined") {
  window.ProvideTerminal = ProvideTerminal;
}
