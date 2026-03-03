/**
 * TW2002 Terminal - xterm.js WebSocket client with themed frame and settings.
 *
 * Provides a centered terminal emulator with CRT/BBS/Glass frame themes,
 * configurable size, font, colors, and effects. Settings persist in localStorage.
 */
'use strict';

(() => {
  // ── Defaults ─────────────────────────────────────────
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
  };

  // Theme-specific default effects
  const THEME_DEFAULTS = {
    crt:   { scanlines: true,  vignette: true,  glow: false },
    bbs:   { scanlines: false, vignette: false, glow: false },
    glass: { scanlines: false, vignette: false, glow: true },
  };

  // ── State ────────────────────────────────────────────
  let term = null;
  let fitAddon = null;
  let ws = null;
  let connected = false;
  let waitingForReconnect = false;
  let settings = {};
  let sessionId = 'web-' + Math.random().toString(36).slice(2, 10);

  // ── Settings Persistence ─────────────────────────────
  const STORAGE_KEY = 'tw2002-terminal-settings';

  function loadSettings() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        settings = { ...DEFAULTS, ...JSON.parse(saved) };
      } else {
        settings = { ...DEFAULTS };
      }
    } catch {
      settings = { ...DEFAULTS };
    }
  }

  function saveSettings() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch {
      // localStorage might be unavailable
    }
  }

  // ── Frame Builders ───────────────────────────────────
  // Each builder returns an HTML string for the frame structure.
  // The terminal div (#terminal) is always inside .screen-inset.

  function buildCRTFrame() {
    return `
      <div class="terminal-frame">
        <div class="screen-inset">
          <div id="terminal"></div>
        </div>
        <div class="frame-bottom">
          <span class="frame-label">Warp Agent Runtime Platform</span>
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="frame-status">
              <span class="status-dot" id="statusDot"></span>
              <span id="statusText">Connecting...</span>
            </div>
            <div class="led" id="ledIndicator"></div>
          </div>
        </div>
      </div>`;
  }

  function buildBBSFrame() {
    return `
      <div class="terminal-frame">
        <div class="frame-header">
          <span class="frame-header-title">WARP AGENT RUNTIME PLATFORM</span>
          <div class="frame-status">
            <span class="status-dot" id="statusDot"></span>
            <span id="statusText">Connecting...</span>
          </div>
        </div>
        <div class="screen-inset">
          <div id="terminal"></div>
        </div>
        <div class="frame-statusbar">
          <span>ANSI Terminal</span>
          <span id="connectionInfo">${settings.cols}×${settings.rows}</span>
        </div>
      </div>`;
  }

  function buildGlassFrame() {
    return `
      <div class="terminal-frame">
        <div class="frame-titlebar">WARP AGENT RUNTIME PLATFORM</div>
        <div class="screen-inset">
          <div id="terminal"></div>
        </div>
        <div class="frame-statusbar">
          <div class="frame-status">
            <span class="status-dot" id="statusDot"></span>
            <span id="statusText">Connecting...</span>
          </div>
          <span id="connectionInfo">${settings.cols}×${settings.rows}</span>
        </div>
      </div>`;
  }

  // ── Apply Settings to DOM ────────────────────────────
  function applyThemeClasses() {
    const body = document.body;
    // Remove old theme/fx classes
    body.className = '';
    body.classList.add('theme-' + settings.theme);
    if (settings.scanlines) body.classList.add('fx-scanlines');
    if (settings.vignette) body.classList.add('fx-vignette');
    if (settings.glow) body.classList.add('fx-glow');
  }

  function applyColors() {
    document.documentElement.style.setProperty('--bg-page', settings.pageBg);
    document.documentElement.style.setProperty('--bg-terminal', settings.termBg);
    document.body.style.background = settings.pageBg;
  }

  function applySettingsToUI() {
    // Sync settings panel controls to current settings
    document.querySelectorAll('.theme-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.theme === settings.theme);
    });
    document.getElementById('setCols').value = settings.cols;
    document.getElementById('valCols').textContent = settings.cols;
    document.getElementById('setRows').value = settings.rows;
    document.getElementById('valRows').textContent = settings.rows;
    document.getElementById('setFontSize').value = settings.fontSize;
    document.getElementById('valFontSize').textContent = settings.fontSize + 'px';
    document.getElementById('setPageBg').value = settings.pageBg;
    document.getElementById('setTermBg').value = settings.termBg;
    document.getElementById('fxScanlines').checked = settings.scanlines;
    document.getElementById('fxVignette').checked = settings.vignette;
    document.getElementById('fxGlow').checked = settings.glow;
  }

  // ── Terminal Creation ────────────────────────────────

  /**
   * Scale font down if needed so xterm renders at least minCols columns,
   * then fit the terminal to the container. Uses xterm's canvas renderer
   * metrics via proposeDimensions() — no DOM span, no font-loading race.
   */
  function fitWithMinCols(minCols) {
    if (!fitAddon || !term) return;
    const proposed = fitAddon.proposeDimensions();
    if (!proposed || proposed.cols <= 0) return;
    if (proposed.cols < minCols) {
      const newSize = Math.max(6, Math.floor(term.options.fontSize * proposed.cols / minCols));
      term.options.fontSize = newSize;
    }
    fitAddon.fit();
  }

  function createTerminal() {
    const frameRoot = document.getElementById('frameRoot');

    // Build frame HTML based on theme
    const builders = { crt: buildCRTFrame, bbs: buildBBSFrame, glass: buildGlassFrame };
    frameRoot.innerHTML = (builders[settings.theme] || buildCRTFrame)();

    applyThemeClasses();
    applyColors();

    const fontFamily = "'Fira Code', 'DejaVu Sans Mono', 'Consolas', monospace";
    const fontSize = settings.fontSize;

    // Create xterm instance
    term = new Terminal({
      theme: {
        background: settings.termBg,
        foreground: '#e2e8f0',
        cursor: '#22c55e',
        cursorAccent: settings.termBg,
        selection: 'rgba(34, 197, 94, 0.15)',
        black: '#000000',
      },
      fontFamily,
      fontSize,
      fontWeight: 'normal',
      letterSpacing: 0,
      lineHeight: 1.2,
      allowTransparency: true,
    });

    const terminalDiv = document.getElementById('terminal');
    term.open(terminalDiv);

    // FitAddon: resize xterm.js to fill the container
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);

    // Defer one frame so CSS layout settles, then scale font to fit minCols
    requestAnimationFrame(() => fitWithMinCols(settings.cols || 80));

    // Re-fit with font scale on every container resize (keyboard, orientation, window)
    const ro = new ResizeObserver(() => fitWithMinCols(settings.cols || 80));
    ro.observe(terminalDiv);

    term.focus();

    // Handle user input
    term.onData(handleTerminalInput);

    // Ctrl key passthrough
    term.attachCustomKeyEventHandler((event) => {
      if (event.ctrlKey || event.metaKey) return false;
      return true;
    });

    // Hide loading on first data
    let firstData = false;
    const originalWrite = term.write.bind(term);
    term.write = function(...args) {
      if (!firstData) {
        document.getElementById('loadingScreen').style.display = 'none';
        firstData = true;
      }
      return originalWrite(...args);
    };

    // Update LED if CRT theme
    updateStatus(connected);
  }

  function recreateTerminal() {
    // Dispose old terminal
    if (term) {
      term.dispose();
      term = null;
    }
    fitAddon = null;
    createTerminal();

    // Re-attach to existing WebSocket if connected
    if (ws && ws.readyState === WebSocket.OPEN) {
      // Terminal is fresh — user will see new output from here
      updateStatus(true);
    }
  }

  // ── Settings Panel ───────────────────────────────────
  function openSettings() {
    document.getElementById('settingsPanel').classList.add('open');
    document.getElementById('settingsOverlay').classList.add('open');
    applySettingsToUI();
  }

  function closeSettings() {
    document.getElementById('settingsPanel').classList.remove('open');
    document.getElementById('settingsOverlay').classList.remove('open');
    // Re-focus terminal
    if (term) term.focus();
  }

  function toggleSettings() {
    const panel = document.getElementById('settingsPanel');
    if (panel.classList.contains('open')) {
      closeSettings();
    } else {
      openSettings();
    }
  }

  function bindSettingsEvents() {
    // Gear button
    document.getElementById('gearBtn').addEventListener('click', toggleSettings);

    // Overlay click-to-close
    document.getElementById('settingsOverlay').addEventListener('click', closeSettings);

    // Theme buttons
    document.querySelectorAll('.theme-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const newTheme = btn.dataset.theme;
        if (newTheme !== settings.theme) {
          // Apply theme-specific default effects when switching themes
          const fx = THEME_DEFAULTS[newTheme] || {};
          settings.theme = newTheme;
          settings.scanlines = fx.scanlines;
          settings.vignette = fx.vignette;
          settings.glow = fx.glow;
          saveSettings();
          recreateTerminal();
          applySettingsToUI();
        }
      });
    });

    // Sliders
    const colsSlider = document.getElementById('setCols');
    const rowsSlider = document.getElementById('setRows');
    const fontSlider = document.getElementById('setFontSize');

    colsSlider.addEventListener('input', () => {
      document.getElementById('valCols').textContent = colsSlider.value;
    });
    colsSlider.addEventListener('change', () => {
      settings.cols = parseInt(colsSlider.value);
      saveSettings();
      recreateTerminal();
    });

    rowsSlider.addEventListener('input', () => {
      document.getElementById('valRows').textContent = rowsSlider.value;
    });
    rowsSlider.addEventListener('change', () => {
      settings.rows = parseInt(rowsSlider.value);
      saveSettings();
      recreateTerminal();
    });

    fontSlider.addEventListener('input', () => {
      document.getElementById('valFontSize').textContent = fontSlider.value + 'px';
    });
    fontSlider.addEventListener('change', () => {
      settings.fontSize = parseInt(fontSlider.value);
      saveSettings();
      recreateTerminal();
    });

    // Color pickers
    document.getElementById('setPageBg').addEventListener('input', (e) => {
      settings.pageBg = e.target.value;
      applyColors();
      saveSettings();
    });
    document.getElementById('setTermBg').addEventListener('input', (e) => {
      settings.termBg = e.target.value;
      saveSettings();
      // Update terminal theme live
      if (term) {
        term.options.theme = { ...term.options.theme, background: settings.termBg, cursorAccent: settings.termBg };
      }
    });

    // Effect checkboxes
    document.getElementById('fxScanlines').addEventListener('change', (e) => {
      settings.scanlines = e.target.checked;
      applyThemeClasses();
      saveSettings();
    });
    document.getElementById('fxVignette').addEventListener('change', (e) => {
      settings.vignette = e.target.checked;
      applyThemeClasses();
      saveSettings();
    });
    document.getElementById('fxGlow').addEventListener('change', (e) => {
      settings.glow = e.target.checked;
      applyThemeClasses();
      saveSettings();
    });
  }

  // ── WebSocket Connection ─────────────────────────────
  function connectWebSocket() {
    try {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${proto}//${location.host}/ws`;

      ws = new WebSocket(wsUrl);
      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        connected = true;
        updateStatus(true);
      };

      ws.onmessage = (event) => {
        try {
          const data = event.data;
          if (data instanceof ArrayBuffer) {
            const view = new Uint8Array(data);
            const text = String.fromCharCode.apply(null, view);
            term.write(text);
          } else {
            term.write(data);
          }
        } catch (e) {
          console.error('Error handling message:', e);
        }
      };

      ws.onerror = (event) => {
        console.error('WebSocket error:', event);
        if (term) term.write('\x1b[31m✗ WebSocket error\x1b[0m\r\n');
      };

      ws.onclose = () => {
        connected = false;
        updateStatus(false);
        if (term) {
          term.write('\r\n\x1b[31m✗ Connection closed\x1b[0m\r\n');
          term.write('\x1b[33mPress any key to reconnect...\x1b[0m');
          waitingForReconnect = true;
        }
      };
    } catch (e) {
      console.error('Failed to create WebSocket:', e);
      if (term) term.write(`\x1b[31m✗ Failed to connect: ${e.message}\x1b[0m\r\n`);
    }
  }

  // ── Terminal Input Handler ───────────────────────────
  function handleTerminalInput(data) {
    // Reconnect on any keypress when disconnected
    if (waitingForReconnect) {
      waitingForReconnect = false;
      if (term) term.write('\r\n\x1b[33m⟳ Reconnecting...\x1b[0m\r\n');
      connectWebSocket();
      return;
    }
    if (!connected || !ws) return;
    try {
      ws.send(data);
    } catch (e) {
      console.error('Failed to send data:', e);
      if (term) term.write(`\x1b[31m✗ Failed to send input: ${e.message}\x1b[0m\r\n`);
    }
  }

  // ── Status Updates ───────────────────────────────────
  function updateStatus(isConnected) {
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    const led = document.getElementById('ledIndicator');

    if (dot) {
      dot.classList.toggle('connected', isConnected);
    }
    if (text) {
      text.textContent = isConnected ? 'Connected' : 'Disconnected';
    }
    if (led) {
      led.classList.toggle('on', isConnected);
    }

    // Update connectionInfo if it exists
    const info = document.getElementById('connectionInfo');
    if (info) {
      info.textContent = isConnected
        ? `${settings.cols}×${settings.rows}`
        : `${settings.cols}×${settings.rows}`;
    }
  }

  // ── Initialization ───────────────────────────────────
  function init() {
    loadSettings();
    bindSettingsEvents();
    createTerminal();
    connectWebSocket();

    // Re-fit once web fonts load — initial fit may have used fallback font metrics
    document.fonts.ready.then(() =>
      requestAnimationFrame(() => fitWithMinCols(settings.cols || 80))
    );
  }

  // ── Startup ──────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
