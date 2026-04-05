//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

type ThemeName = "crt" | "bbs" | "glass" | "code";

interface TerminalConfig {
  wsUrl?: string;
  theme?: ThemeName;
  cols?: number;
  rows?: number;
  fontSize?: number;
  pageBg?: string;
  termBg?: string;
  scanlines?: boolean;
  vignette?: boolean;
  glow?: boolean;
  storageKey?: string;
  title?: string | null;
}

interface TerminalSettings {
  theme: ThemeName;
  cols: number;
  rows: number;
  fontSize: number;
  pageBg: string;
  termBg: string;
  scanlines: boolean;
  vignette: boolean;
  glow: boolean;
  storageKey: string;
  title: string | null;
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

// biome-ignore lint/correctness/noUnusedVariables: script-mode global Window augmentation used throughout
interface Window {
  Terminal?: XtermCtor;
  FitAddon?: FitAddonGlobal;
  ProvideTerminal?: typeof ProvideTerminal;
  demoTerminal?: ProvideTerminal;
}

const DEFAULTS: TerminalSettings = {
  theme: "code",
  cols: 80,
  rows: 25,
  fontSize: 14,
  pageBg: "#0a0a0a",
  termBg: "#0a0a0a",
  scanlines: true,
  vignette: true,
  glow: false,
  storageKey: "provide-terminal-settings",
  title: null,
};

const THEME_DEFAULTS: Record<ThemeName, Pick<TerminalSettings, "scanlines" | "vignette" | "glow">> = {
  crt: { scanlines: true, vignette: true, glow: false },
  bbs: { scanlines: false, vignette: false, glow: false },
  glass: { scanlines: false, vignette: false, glow: true },
  code: { scanlines: false, vignette: false, glow: false },
};

let cssInjected = false;
let instanceCount = 0;

const scriptEl =
  typeof document !== "undefined" && document.currentScript instanceof HTMLScriptElement
    ? document.currentScript
    : null;

function injectCss(): void {
  if (cssInjected) return;
  cssInjected = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = scriptEl?.src ? `${scriptEl.src.replace(/[^/]*$/, "")}terminal.css` : "terminal.css";
  document.head.appendChild(link);
}

function asThemeName(value: string | null | undefined): ThemeName {
  return value === "bbs" || value === "glass" || value === "crt" ? value : "code";
}

class ProvideTerminal {
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

  constructor(container: HTMLElement, config: TerminalConfig = {}) {
    this.container = container;
    this.config = { ...DEFAULTS, ...config };
    this.uid = ++instanceCount;
    injectCss();
    this.buildDom();
    this.loadSettings();
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
    const gearSvg =
      '<svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1112 8.4a3.6 3.6 0 010 7.2z"/></svg>';
    const root = document.createElement("div");
    root.className = "provide-terminal";
    root.innerHTML = `
      <button class="gear-btn" id="gearBtn-${this.uid}" title="Settings">${gearSvg}</button>
      <div class="settings-overlay" id="settingsOverlay-${this.uid}"></div>
      <div class="settings-panel" id="settingsPanel-${this.uid}">
        <h3>Theme</h3>
        <div class="theme-options">
          <button class="theme-btn" data-theme="code">Code</button>
          <button class="theme-btn" data-theme="crt">CRT</button>
          <button class="theme-btn" data-theme="bbs">BBS/DOS</button>
          <button class="theme-btn" data-theme="glass">Glass</button>
        </div>
        <h3>Terminal Size</h3>
        <div class="setting-row">
          <label>Columns</label>
          <input type="range" id="setCols-${this.uid}" min="80" max="120" value="80">
          <span class="val" id="valCols-${this.uid}">80</span>
        </div>
        <div class="setting-row">
          <label>Rows</label>
          <input type="range" id="setRows-${this.uid}" min="25" max="40" value="25">
          <span class="val" id="valRows-${this.uid}">25</span>
        </div>
        <div class="setting-row">
          <label>Font Size</label>
          <input type="range" id="setFontSize-${this.uid}" min="11" max="18" value="14">
          <span class="val" id="valFontSize-${this.uid}">14px</span>
        </div>
        <h3>Colors</h3>
        <div class="setting-row">
          <label>Page Background</label>
          <input type="color" id="setPageBg-${this.uid}" value="#0a0a0a">
        </div>
        <div class="setting-row">
          <label>Terminal Background</label>
          <input type="color" id="setTermBg-${this.uid}" value="#0a0a0a">
        </div>
        <h3>Effects</h3>
        <div class="setting-row">
          <label>Scanlines</label>
          <input type="checkbox" id="fxScanlines-${this.uid}">
        </div>
        <div class="setting-row">
          <label>Vignette</label>
          <input type="checkbox" id="fxVignette-${this.uid}">
        </div>
        <div class="setting-row">
          <label>Glow</label>
          <input type="checkbox" id="fxGlow-${this.uid}">
        </div>
      </div>
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
            <span class="status-dot" data-status-dot="1"></span>
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
              <span class="status-dot" data-status-dot="1"></span>
              <span data-status-text="1">Connecting...</span>
            </div>
            <span data-connection-info="1">${this.settings.cols}×${this.settings.rows}</span>
          </div>
        </div>
        <div class="frame-bottom">
          <span class="frame-label">${label}</span>
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="frame-status">
              <span class="status-dot" data-status-dot="1"></span>
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
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      this.updateStatus(true);
    };
    ws.onmessage = (event) => {
      const payload = typeof event.data === "string" ? event.data : "";
      if (!payload || this.term === null) return;
      this.term.write(payload);
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

  private loadSettings(): void {
    const key = this.config.storageKey;
    const base: TerminalSettings = { ...DEFAULTS, ...this.config, theme: asThemeName(this.config.theme) };
    try {
      const raw = localStorage.getItem(key);
      if (!raw) {
        this.settings = base;
        return;
      }
      const parsed = JSON.parse(raw) as Partial<TerminalSettings>;
      this.settings = {
        ...base,
        ...parsed,
        theme: asThemeName(parsed.theme ?? base.theme),
      };
    } catch {
      this.settings = base;
    }
  }

  private saveSettings(): void {
    localStorage.setItem(this.config.storageKey, JSON.stringify(this.settings));
  }

  private applyThemeClasses(): void {
    if (this.root === null) return;
    this.root.classList.remove(
      "theme-crt",
      "theme-bbs",
      "theme-glass",
      "theme-code",
      "fx-scanlines",
      "fx-vignette",
      "fx-glow",
    );
    this.root.classList.add(`theme-${this.settings.theme}`);
    if (this.settings.scanlines) this.root.classList.add("fx-scanlines");
    if (this.settings.vignette) this.root.classList.add("fx-vignette");
    if (this.settings.glow) this.root.classList.add("fx-glow");
  }

  private applyColors(): void {
    if (this.root === null) return;
    this.root.style.setProperty("--bg-page", this.settings.pageBg);
    this.root.style.setProperty("--bg-terminal", this.settings.termBg);
    this.root.style.background = this.settings.pageBg;
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
    this.applyThemeClasses();
    this.applyColors();
    this.applySettingsToUi();
    this.saveSettings();
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
        const nextTheme = asThemeName(node.dataset.theme);
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
    this.applyThemeClasses();
    this.applyColors();

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
    this.term.attachCustomKeyEventHandler((event) => !(event.ctrlKey || event.metaKey));

    const loading = this.q<HTMLElement>("loadingScreen");
    loading.style.removeProperty("display");
    let firstData = false;
    const originalWrite = this.term.write.bind(this.term);
    this.term.write = (data: string): void => {
      if (!firstData) {
        loading.style.display = "none";
        firstData = true;
      }
      originalWrite(data);
    };

    this.updateStatus(this.connected);
  }
}

if (typeof window !== "undefined") {
  window.ProvideTerminal = ProvideTerminal;
}
