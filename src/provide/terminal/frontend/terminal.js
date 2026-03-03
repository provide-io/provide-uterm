/**
 * ProvideTerminal - Embeddable xterm.js WebSocket terminal widget.
 *
 * Usage:
 *   const term = new ProvideTerminal(containerEl, { wsUrl: '/ws/terminal' });
 *   term.connect();    // open WS (called automatically on construction)
 *   term.disconnect(); // close WS
 *   term.dispose();    // tear down entirely
 */
'use strict';

// ── Module-level guards ───────────────────────────────────────────────────────
let _cssInjected = false;
let _instanceCount = 0;
// Capture script element synchronously (available only during initial parse)
const _scriptEl = typeof document !== 'undefined' ? document.currentScript : null;

// ── Defaults ─────────────────────────────────────────────────────────────────
const DEFAULTS = {
  theme: 'crt',
  cols: 80,
  rows: 25,
  fontSize: 14,
  pageBg: '#0a0a0a',
  termBg: '#0a0a0a',
  scanlines: true,
  vignette: true,
  glow: false,
  storageKey: 'provide-terminal-settings',
  title: null,
};

// Theme-specific default effects
const THEME_DEFAULTS = {
  crt:   { scanlines: true,  vignette: true,  glow: false },
  bbs:   { scanlines: false, vignette: false, glow: false },
  glass: { scanlines: false, vignette: false, glow: true },
};


// ── CSS injection ─────────────────────────────────────────────────────────────
function _injectCSS() {
  if (_cssInjected) return;
  _cssInjected = true;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = (_scriptEl && _scriptEl.src)
    ? _scriptEl.src.replace(/[^/]*$/, '') + 'terminal.css'
    : 'terminal.css';
  document.head.appendChild(link);
}

// ── ProvideTerminal class ───────────────────────────────────────────────────────
class ProvideTerminal {
  /**
   * Create an embeddable terminal widget.
   *
   * @param {HTMLElement} container - Element to mount the terminal into.
   * @param {object} [config={}] - Configuration options.
   * @param {string} [config.wsUrl] - WebSocket endpoint. Defaults to auto-detect.
   * @param {string} [config.theme='crt'] - 'crt' | 'bbs' | 'glass'
   * @param {number} [config.cols=80]
   * @param {number} [config.rows=25]
   * @param {number} [config.fontSize=14]
   * @param {string} [config.pageBg='#0a0a0a']
   * @param {string} [config.termBg='#0a0a0a']
   * @param {boolean} [config.scanlines=true]
   * @param {boolean} [config.vignette=true]
   * @param {boolean} [config.glow=false]
   * @param {string} [config.storageKey='provide-terminal-settings']
   * @param {string|null} [config.title=null] - Override frame branding text.
   * @param {number} [config.heartbeatMs=25000] - Keepalive ping interval (ms). 0 disables.
   */
  constructor(container, config = {}) {
    this._container = container;
    this._config = { ...DEFAULTS, ...config };
    this._uid = ++_instanceCount;

    // Instance state
    this._term = null;
    this._fitAddon = null;
    this._ws = null;
    this._connected = false;
    this._waitingForReconnect = false;
    this._settings = {};
    this._root = null;
    this._ro = null;

    _injectCSS();
    this._buildDOM();
    this._loadSettings();
    this._bindSettingsEvents();
    this._createTerminal();
    this.connect();

    // Re-fit once web fonts load — initial fit may have used fallback font metrics
    document.fonts.ready.then(() =>
      requestAnimationFrame(() => this._fitWithMinCols(this._settings.cols || 80))
    );
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  /** Open the WebSocket connection. Called automatically on construction. */
  connect() {
    this._connectWebSocket();
  }

  /** Close the WebSocket connection. */
  disconnect() {
    this._waitingForReconnect = false;
    if (this._ws) {
      this._ws.close();
      this._ws = null;
    }
  }

  /** Tear down entirely: xterm, WebSocket, ResizeObserver, and DOM. */
  dispose() {
    this.disconnect();
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    if (this._term) { this._term.dispose(); this._term = null; }
    this._fitAddon = null;
    if (this._root && this._root.parentNode) {
      this._root.parentNode.removeChild(this._root);
    }
    this._root = null;
  }

  // ── Internal helpers ────────────────────────────────────────────────────────

  /** Query by ID within this instance's root (IDs are suffixed with uid). */
  _q(id) {
    return this._root.querySelector('#' + id + '-' + this._uid);
  }

  // ── DOM Construction ────────────────────────────────────────────────────────

  _buildDOM() {
    const uid = this._uid;
    const GEAR_SVG = `<svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1112 8.4a3.6 3.6 0 010 7.2z"/></svg>`;

    const root = document.createElement('div');
    root.className = 'provide-terminal';
    root.innerHTML = `
      <button class="gear-btn" id="gearBtn-${uid}" title="Settings">${GEAR_SVG}</button>
      <div class="settings-overlay" id="settingsOverlay-${uid}"></div>
      <div class="settings-panel" id="settingsPanel-${uid}">
        <h3>Theme</h3>
        <div class="theme-options">
          <button class="theme-btn" data-theme="crt">CRT</button>
          <button class="theme-btn" data-theme="bbs">BBS/DOS</button>
          <button class="theme-btn" data-theme="glass">Glass</button>
        </div>
        <h3>Terminal Size</h3>
        <div class="setting-row">
          <label>Columns</label>
          <input type="range" id="setCols-${uid}" min="80" max="120" value="80">
          <span class="val" id="valCols-${uid}">80</span>
        </div>
        <div class="setting-row">
          <label>Rows</label>
          <input type="range" id="setRows-${uid}" min="25" max="40" value="25">
          <span class="val" id="valRows-${uid}">25</span>
        </div>
        <div class="setting-row">
          <label>Font Size</label>
          <input type="range" id="setFontSize-${uid}" min="11" max="18" value="14">
          <span class="val" id="valFontSize-${uid}">14px</span>
        </div>
        <h3>Colors</h3>
        <div class="setting-row">
          <label>Page Background</label>
          <input type="color" id="setPageBg-${uid}" value="#0a0a0a">
        </div>
        <div class="setting-row">
          <label>Terminal Background</label>
          <input type="color" id="setTermBg-${uid}" value="#0a0a0a">
        </div>
        <h3>Effects</h3>
        <div class="setting-row">
          <label>Scanlines</label>
          <input type="checkbox" id="fxScanlines-${uid}">
        </div>
        <div class="setting-row">
          <label>Vignette</label>
          <input type="checkbox" id="fxVignette-${uid}">
        </div>
        <div class="setting-row">
          <label>Glow</label>
          <input type="checkbox" id="fxGlow-${uid}">
        </div>
      </div>
      <div class="page-wrapper" id="pageWrapper-${uid}">
        <div class="frame-root" id="frameRoot-${uid}"></div>
        <div class="loading" id="loadingScreen-${uid}">
          <div>
            <div class="loading-spinner"></div>
            Initializing Terminal Connection...
          </div>
        </div>
      </div>
    `;

    this._root = root;
    this._container.appendChild(root);
  }

  // ── Frame Builders ────────────────────────────────────────────────────────

  /** HTML-escape a string so it is safe to render via textContent assignment. */
  _escHtml(s) {
    const el = document.createElement('span');
    el.textContent = String(s);
    return el.innerHTML;
  }

  _buildCRTFrame() {
    const uid = this._uid;
    // Use _escHtml to prevent XSS when title comes from config/URL params.
    const label = this._escHtml(this._config.title || 'Warp Agent Runtime Platform');
    return `
      <div class="terminal-frame">
        <div class="screen-inset">
          <div class="terminal-div" id="terminalDiv-${uid}"></div>
        </div>
        <div class="frame-bottom">
          <span class="frame-label">${label}</span>
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="frame-status">
              <span class="status-dot" id="statusDot-${uid}"></span>
              <span id="statusText-${uid}">Connecting...</span>
            </div>
            <div class="led" id="ledIndicator-${uid}"></div>
          </div>
        </div>
      </div>`;
  }

  _buildBBSFrame() {
    const uid = this._uid;
    const title = this._escHtml((this._config.title || 'Warp Agent Runtime Platform').toUpperCase());
    return `
      <div class="terminal-frame">
        <div class="frame-header">
          <span class="frame-header-title">${title}</span>
          <div class="frame-status">
            <span class="status-dot" id="statusDot-${uid}"></span>
            <span id="statusText-${uid}">Connecting...</span>
          </div>
        </div>
        <div class="screen-inset">
          <div class="terminal-div" id="terminalDiv-${uid}"></div>
        </div>
        <div class="frame-statusbar">
          <span>ANSI Terminal</span>
          <span id="connectionInfo-${uid}">${this._settings.cols}×${this._settings.rows}</span>
        </div>
      </div>`;
  }

  _buildGlassFrame() {
    const uid = this._uid;
    const title = this._escHtml((this._config.title || 'Warp Agent Runtime Platform').toUpperCase());
    return `
      <div class="terminal-frame">
        <div class="frame-titlebar">${title}</div>
        <div class="screen-inset">
          <div class="terminal-div" id="terminalDiv-${uid}"></div>
        </div>
        <div class="frame-statusbar">
          <div class="frame-status">
            <span class="status-dot" id="statusDot-${uid}"></span>
            <span id="statusText-${uid}">Connecting...</span>
          </div>
          <span id="connectionInfo-${uid}">${this._settings.cols}×${this._settings.rows}</span>
        </div>
      </div>`;
  }

  // ── Settings Persistence ─────────────────────────────────────────────────

  _loadSettings() {
    const key = this._config.storageKey;
    // Base: DEFAULTS overridden by any UI-relevant constructor config
    const base = { ...DEFAULTS, ...this._config };
    try {
      const saved = localStorage.getItem(key);
      this._settings = saved ? { ...base, ...JSON.parse(saved) } : { ...base };
    } catch {
      this._settings = { ...base };
    }
  }

  _saveSettings() {
    try {
      localStorage.setItem(this._config.storageKey, JSON.stringify(this._settings));
    } catch {
      // localStorage might be unavailable
    }
  }

  // ── Apply Settings to DOM ─────────────────────────────────────────────────

  _applyThemeClasses() {
    const root = this._root;
    root.classList.remove('theme-crt', 'theme-bbs', 'theme-glass');
    root.classList.remove('fx-scanlines', 'fx-vignette', 'fx-glow');
    root.classList.add('theme-' + this._settings.theme);
    if (this._settings.scanlines) root.classList.add('fx-scanlines');
    if (this._settings.vignette) root.classList.add('fx-vignette');
    if (this._settings.glow) root.classList.add('fx-glow');
  }

  _applyColors() {
    this._root.style.setProperty('--bg-page', this._settings.pageBg);
    this._root.style.setProperty('--bg-terminal', this._settings.termBg);
    this._root.style.background = this._settings.pageBg;
  }

  _applySettingsToUI() {
    this._root.querySelectorAll('.theme-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.theme === this._settings.theme);
    });
    this._q('setCols').value = this._settings.cols;
    this._q('valCols').textContent = this._settings.cols;
    this._q('setRows').value = this._settings.rows;
    this._q('valRows').textContent = this._settings.rows;
    this._q('setFontSize').value = this._settings.fontSize;
    this._q('valFontSize').textContent = this._settings.fontSize + 'px';
    this._q('setPageBg').value = this._settings.pageBg;
    this._q('setTermBg').value = this._settings.termBg;
    this._q('fxScanlines').checked = this._settings.scanlines;
    this._q('fxVignette').checked = this._settings.vignette;
    this._q('fxGlow').checked = this._settings.glow;
  }

  // ── Terminal Creation ─────────────────────────────────────────────────────

  /**
   * Scale font down if needed so xterm renders at least minCols columns,
   * then fit the terminal to the container.
   */
  _fitWithMinCols(minCols) {
    if (!this._fitAddon || !this._term) return;
    const proposed = this._fitAddon.proposeDimensions();
    if (!proposed || proposed.cols <= 0) return;
    if (proposed.cols < minCols) {
      const newSize = Math.max(6, Math.floor(this._term.options.fontSize * proposed.cols / minCols));
      this._term.options.fontSize = newSize;
    }
    this._fitAddon.fit();
  }

  _createTerminal() {
    if (typeof Terminal === 'undefined') {
      throw new Error('xterm.js (Terminal) not loaded — include @xterm/xterm before terminal.js');
    }
    if (typeof FitAddon === 'undefined') {
      throw new Error('xterm addon-fit (FitAddon) not loaded — include @xterm/addon-fit before terminal.js');
    }
    const frameRoot = this._q('frameRoot');
    const builders = {
      crt:   () => this._buildCRTFrame(),
      bbs:   () => this._buildBBSFrame(),
      glass: () => this._buildGlassFrame(),
    };
    frameRoot.innerHTML = (builders[this._settings.theme] || builders.crt)();

    this._applyThemeClasses();
    this._applyColors();

    const fontFamily = "'Fira Code', 'DejaVu Sans Mono', 'Consolas', monospace";

    this._term = new Terminal({
      theme: {
        background: this._settings.termBg,
        foreground: '#e2e8f0',
        cursor: '#22c55e',
        cursorAccent: this._settings.termBg,
        selection: 'rgba(34, 197, 94, 0.15)',
        black: '#000000',
      },
      fontFamily,
      fontSize: this._settings.fontSize,
      fontWeight: 'normal',
      letterSpacing: 0,
      lineHeight: 1.2,
      allowTransparency: true,
    });

    const terminalDiv = this._q('terminalDiv');
    this._term.open(terminalDiv);

    this._fitAddon = new FitAddon.FitAddon();
    this._term.loadAddon(this._fitAddon);

    requestAnimationFrame(() => this._fitWithMinCols(this._settings.cols || 80));

    // Re-fit with font scale on every container resize
    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver(() => this._fitWithMinCols(this._settings.cols || 80));
    this._ro.observe(terminalDiv);

    this._term.focus();
    this._term.onData((data) => this._handleTerminalInput(data));

    // Ctrl key passthrough
    this._term.attachCustomKeyEventHandler((event) => {
      if (event.ctrlKey || event.metaKey) return false;
      return true;
    });

    // Hide loading screen on first data received
    const loadingScreen = this._q('loadingScreen');
    if (loadingScreen) loadingScreen.style.removeProperty('display');
    let firstData = false;
    const originalWrite = this._term.write.bind(this._term);
    this._term.write = (...args) => {
      if (!firstData) {
        if (loadingScreen) loadingScreen.style.display = 'none';
        firstData = true;
      }
      return originalWrite(...args);
    };

    this._updateStatus(this._connected);
  }

  _recreateTerminal() {
    if (this._term) { this._term.dispose(); this._term = null; }
    this._fitAddon = null;
    this._createTerminal();
    // Hide loading immediately on recreation (already connected)
    const loadingScreen = this._q('loadingScreen');
    if (loadingScreen) loadingScreen.style.display = 'none';
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._updateStatus(true);
    }
  }
}

// ── Global exposure for CDN / script-tag use ──────────────────────────────────
// Exposed here so terminal-panel.js can safely reference ProvideTerminal.prototype.
if (typeof window !== 'undefined') window.ProvideTerminal = ProvideTerminal;
