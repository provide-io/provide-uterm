//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { LitElement, PropertyValues, html } from "lit";
import { property } from "lit/decorators.js";
import { unsafeHTML } from "lit/directives/unsafe-html.js";
import { buildSettingsPanelHtml, DEFAULTS, loadSettings, saveSettings, type TerminalConfig, type TerminalSettings } from "./terminal-settings.js";
import { applyColors, applyThemeClasses, asThemeName, THEME_DEFAULTS, type ThemeName } from "./terminal-themes.js";

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

  override render() {
    return html`
      ${unsafeHTML(buildSettingsPanelHtml(this.uid))}
      <div class="page-wrapper" id="pageWrapper-${this.uid}">
        <div class="frame-root" id="frameRoot-${this.uid}">
          ${unsafeHTML(this.buildFrame())}
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

    this._firstDataWritten = false;
    const loading = this.q<HTMLElement>("loadingScreen");
    loading.style.removeProperty("display");

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
    this.dispatchEvent(new CustomEvent("uterm-data", { detail: data }));
  }

  // --- Public API ---

  public writeData(data: string): void {
    if (!this._firstDataWritten) {
      const loading = this.querySelector(`#loadingScreen-${this.uid}`) as HTMLElement | null;
      if (loading) loading.style.display = "none";
      this._firstDataWritten = true;
    }
    this.term?.write(data);
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
    return lines.join("\\n");
  }

  public focusTerminal(): void {
    this.term?.focus();
  }

  public dispose(): void {
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.term?.dispose();
    this.term = null;
    this.fitAddon = null;
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
