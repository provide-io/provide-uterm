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
import { buildSettingsPanelHtml, DEFAULTS, loadSettings, saveSettings, } from "./terminal-settings.js";
import { applyColors, applyThemeClasses, asThemeName, THEME_DEFAULTS, } from "./terminal-themes.js";
let cssInjected = false;
let instanceCount = 0;
function resolveCssHref() {
    // import.meta.url resolves the module location for ES modules; fall back
    // to a relative path when the host environment does not provide it (some
    // test runners hand back a non-URL string under jsdom).
    try {
        return new URL("./terminal.css", import.meta.url).href;
    }
    catch {
        return "terminal.css";
    }
}
function injectCss() {
    if (cssInjected)
        return;
    cssInjected = true;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = resolveCssHref();
    document.head.appendChild(link);
}
export class ProvideTerminal {
    constructor(container, config = {}) {
        this.term = null;
        this.fitAddon = null;
        this.ws = null;
        this.connected = false;
        this.reconnectEnabled = false;
        this.settings = { ...DEFAULTS };
        this.root = null;
        this.resizeObserver = null;
        this.reconnectTimer = null;
        this.wsDecoder = new ControlChannelDecoder();
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
    connect() {
        this.connectWebSocket();
    }
    disconnect() {
        this.reconnectEnabled = false;
        if (this.reconnectTimer !== null) {
            window.clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        this.ws?.close();
        this.ws = null;
    }
    getBufferText(maxLines = 200) {
        if (!this.term)
            return "";
        const active = this.term.buffer.active;
        const end = active.baseY + active.cursorY;
        const start = Math.max(0, end - Math.max(1, maxLines) + 1);
        const lines = [];
        for (let i = start; i <= end; i += 1) {
            const line = active.getLine(i)?.translateToString(true) ?? "";
            if (line.trim())
                lines.push(line);
        }
        return lines.join("\n");
    }
    dispose() {
        this.disconnect();
        this.resizeObserver?.disconnect();
        this.resizeObserver = null;
        this.term?.dispose();
        this.term = null;
        this.fitAddon = null;
        this.root?.parentNode?.removeChild(this.root);
        this.root = null;
    }
    q(id) {
        const node = this.root?.querySelector(`#${id}-${this.uid}`);
        if (!(node instanceof Element)) {
            throw new Error(`Missing terminal element: ${id}`);
        }
        return node;
    }
    buildDom() {
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
    escapeHtml(value) {
        const el = document.createElement("span");
        el.textContent = String(value);
        return el.innerHTML;
    }
    buildFrame() {
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
    resolveWsUrl() {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        if (this.config.wsUrl) {
            return this.config.wsUrl.startsWith("/") ? `${proto}//${location.host}${this.config.wsUrl}` : this.config.wsUrl;
        }
        return `${proto}//${location.host}/ws/terminal`;
    }
    updateStatus(connected) {
        const statusText = connected ? "Connected" : "Disconnected";
        this.root?.querySelectorAll("[data-status-dot='1']").forEach((dot) => {
            dot.className = `status-dot${connected ? " connected" : ""}`;
            dot.setAttribute("aria-label", statusText);
        });
        this.root?.querySelectorAll("[data-led-indicator='1']").forEach((led) => {
            led.classList.toggle("on", connected);
        });
        this.root?.querySelectorAll("[data-status-text='1']").forEach((text) => {
            text.textContent = statusText;
        });
        this.root?.querySelectorAll("[data-connection-info='1']").forEach((info) => {
            info.textContent = `${this.settings.cols}×${this.settings.rows}`;
        });
    }
    handleTerminalInput(data) {
        if (!data || !this.ws || this.ws.readyState !== WebSocket.OPEN)
            return;
        this.ws.send(data);
    }
    scheduleReconnect() {
        if (this.reconnectTimer !== null)
            return;
        this.reconnectTimer = window.setTimeout(() => {
            this.reconnectTimer = null;
            if (this.reconnectEnabled && this.ws === null) {
                this.connectWebSocket();
            }
        }, 1000);
    }
    connectWebSocket() {
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
            if (this.ws !== ws)
                return;
            this.connected = true;
            this.updateStatus(true);
        };
        ws.onmessage = (event) => {
            const payload = typeof event.data === "string" ? event.data : "";
            if (!payload || this.term === null)
                return;
            // Decode the inline control-channel framing so the user never sees raw
            // JSON control frames mixed into terminal output. Control chunks are
            // discarded here — ProvideTerminal has no hijack UI to surface them.
            let frames;
            try {
                frames = this.wsDecoder.feed(payload);
            }
            catch {
                // Stream is corrupt; reset decoder and fall back to writing the raw
                // payload so users don't get stuck on a blank screen.
                this.wsDecoder.reset();
                this.term.write(payload);
                return;
            }
            for (const frame of frames) {
                if (frame.type === "data")
                    this.term.write(frame.data);
            }
        };
        ws.onclose = () => {
            if (this.ws !== ws)
                return;
            this.ws = null;
            this.connected = false;
            this.updateStatus(false);
            if (this.reconnectEnabled)
                this.scheduleReconnect();
        };
        ws.onerror = () => ws.close();
    }
    applySettingsToUi() {
        if (this.root === null)
            return;
        this.root.querySelectorAll(".theme-btn").forEach((node) => {
            if (node instanceof HTMLElement) {
                node.classList.toggle("active", node.dataset.theme === this.settings.theme);
            }
        });
        this.q("setCols").value = String(this.settings.cols);
        this.q("valCols").textContent = String(this.settings.cols);
        this.q("setRows").value = String(this.settings.rows);
        this.q("valRows").textContent = String(this.settings.rows);
        this.q("setFontSize").value = String(this.settings.fontSize);
        this.q("valFontSize").textContent = `${this.settings.fontSize}px`;
        this.q("setPageBg").value = this.settings.pageBg;
        this.q("setTermBg").value = this.settings.termBg;
        this.q("fxScanlines").checked = this.settings.scanlines;
        this.q("fxVignette").checked = this.settings.vignette;
        this.q("fxGlow").checked = this.settings.glow;
    }
    applyRuntimeSettings() {
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
    bindSettingsEvents() {
        const overlay = this.q("settingsOverlay");
        const panel = this.q("settingsPanel");
        const gear = this.q("gearBtn");
        const togglePanel = (open) => {
            panel.classList.toggle("open", open);
            overlay.classList.toggle("open", open);
        };
        gear.addEventListener("click", () => togglePanel(!panel.classList.contains("open")));
        overlay.addEventListener("click", () => togglePanel(false));
        this.root?.querySelectorAll(".theme-btn").forEach((node) => {
            if (!(node instanceof HTMLButtonElement))
                return;
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
        const bindRange = (id, outputId, update, format) => {
            const input = this.q(id);
            const output = this.q(outputId);
            input.addEventListener("input", () => {
                update(input.value);
                output.textContent = format(input.value);
                this.applyRuntimeSettings();
            });
        };
        bindRange("setCols", "valCols", (value) => {
            this.settings.cols = Number(value);
        }, (value) => value);
        bindRange("setRows", "valRows", (value) => {
            this.settings.rows = Number(value);
        }, (value) => value);
        bindRange("setFontSize", "valFontSize", (value) => {
            this.settings.fontSize = Number(value);
        }, (value) => `${value}px`);
        const pageBgInput = this.q("setPageBg");
        pageBgInput.addEventListener("input", () => {
            this.settings.pageBg = pageBgInput.value;
            this.applyRuntimeSettings();
        });
        const termBgInput = this.q("setTermBg");
        termBgInput.addEventListener("input", () => {
            this.settings.termBg = termBgInput.value;
            this.applyRuntimeSettings();
        });
        const bindCheckbox = (id, update) => {
            const input = this.q(id);
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
    fitWithMinCols(minCols) {
        if (this.fitAddon === null || this.term === null)
            return;
        const proposed = this.fitAddon.proposeDimensions();
        if (!proposed || proposed.cols <= 0)
            return;
        if (proposed.cols < minCols) {
            this.term.options.fontSize = Math.max(6, Math.floor((this.term.options.fontSize * proposed.cols) / minCols));
        }
        this.fitAddon.fit();
    }
    createTerminal() {
        if (window.Terminal === undefined) {
            throw new Error("xterm.js (Terminal) not loaded — include @xterm/xterm before terminal.js");
        }
        if (window.FitAddon === undefined) {
            throw new Error("xterm addon-fit (FitAddon) not loaded — include @xterm/addon-fit before terminal.js");
        }
        const frameRoot = this.q("frameRoot");
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
        const terminalDiv = this.q("terminalDiv");
        this.term.open(terminalDiv);
        this.fitAddon = new window.FitAddon.FitAddon();
        this.term.loadAddon(this.fitAddon);
        requestAnimationFrame(() => this.fitWithMinCols(this.settings.cols));
        this.resizeObserver?.disconnect();
        this.resizeObserver = new ResizeObserver(() => this.fitWithMinCols(this.settings.cols));
        this.resizeObserver.observe(terminalDiv);
        this.term.focus();
        this.term.onData((data) => this.handleTerminalInput(data));
        const loading = this.q("loadingScreen");
        loading.style.removeProperty("display");
        let firstData = false;
        const originalWrite = this.term.write.bind(this.term);
        this.term.write = (data) => {
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
