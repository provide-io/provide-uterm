/**
 * ProvideTerminal — settings panel, WebSocket, input, and status methods.
 * Extends ProvideTerminal.prototype. Must be loaded after terminal.js.
 */
'use strict';

Object.assign(ProvideTerminal.prototype, {
  // ── Settings Panel ────────────────────────────────────────────────────────

  _openSettings() {
    this._q('settingsPanel').classList.add('open');
    this._q('settingsOverlay').classList.add('open');
    this._applySettingsToUI();
  },

  _closeSettings() {
    this._q('settingsPanel').classList.remove('open');
    this._q('settingsOverlay').classList.remove('open');
    if (this._term) this._term.focus();
  },

  _toggleSettings() {
    if (this._q('settingsPanel').classList.contains('open')) {
      this._closeSettings();
    } else {
      this._openSettings();
    }
  },

  _bindSettingsEvents() {
    this._q('gearBtn').addEventListener('click', () => this._toggleSettings());
    this._q('settingsOverlay').addEventListener('click', () => this._closeSettings());

    // Theme buttons
    this._root.querySelectorAll('.theme-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const newTheme = btn.dataset.theme;
        if (newTheme !== this._settings.theme) {
          const fx = THEME_DEFAULTS[newTheme] || {};
          this._settings.theme = newTheme;
          this._settings.scanlines = fx.scanlines;
          this._settings.vignette = fx.vignette;
          this._settings.glow = fx.glow;
          this._saveSettings();
          this._recreateTerminal();
          this._applySettingsToUI();
        }
      });
    });

    // Sliders
    const colsSlider = this._q('setCols');
    const rowsSlider = this._q('setRows');
    const fontSlider = this._q('setFontSize');

    colsSlider.addEventListener('input', () => {
      this._q('valCols').textContent = colsSlider.value;
    });
    colsSlider.addEventListener('change', () => {
      this._settings.cols = parseInt(colsSlider.value);
      this._saveSettings();
      this._recreateTerminal();
    });

    rowsSlider.addEventListener('input', () => {
      this._q('valRows').textContent = rowsSlider.value;
    });
    rowsSlider.addEventListener('change', () => {
      this._settings.rows = parseInt(rowsSlider.value);
      this._saveSettings();
      this._recreateTerminal();
    });

    fontSlider.addEventListener('input', () => {
      this._q('valFontSize').textContent = fontSlider.value + 'px';
    });
    fontSlider.addEventListener('change', () => {
      this._settings.fontSize = parseInt(fontSlider.value);
      this._saveSettings();
      this._recreateTerminal();
    });

    // Color pickers
    this._q('setPageBg').addEventListener('input', (e) => {
      this._settings.pageBg = e.target.value;
      this._applyColors();
      this._saveSettings();
    });
    this._q('setTermBg').addEventListener('input', (e) => {
      this._settings.termBg = e.target.value;
      this._saveSettings();
      if (this._term) {
        this._term.options.theme = {
          ...this._term.options.theme,
          background: this._settings.termBg,
          cursorAccent: this._settings.termBg,
        };
      }
    });

    // Effect checkboxes
    this._q('fxScanlines').addEventListener('change', (e) => {
      this._settings.scanlines = e.target.checked;
      this._applyThemeClasses();
      this._saveSettings();
    });
    this._q('fxVignette').addEventListener('change', (e) => {
      this._settings.vignette = e.target.checked;
      this._applyThemeClasses();
      this._saveSettings();
    });
    this._q('fxGlow').addEventListener('change', (e) => {
      this._settings.glow = e.target.checked;
      this._applyThemeClasses();
      this._saveSettings();
    });
  },

  // ── WebSocket Connection ─────────────────────────────────────────────────

  _resolveWsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = this._config.wsUrl;
    if (!url) return `${proto}//${location.host}/ws`;
    if (url.startsWith('/')) return `${proto}//${location.host}${url}`;
    return url; // already absolute ws:// or wss://
  },

  _clearHeartbeatPing() {
    if (this._heartbeatPingTimer) {
      clearInterval(this._heartbeatPingTimer);
      this._heartbeatPingTimer = null;
    }
  },

  _startHeartbeatPing() {
    this._clearHeartbeatPing();
    const ms = (this._config.heartbeatMs != null) ? this._config.heartbeatMs : 25000;
    if (!ms) return;
    this._heartbeatPingTimer = setInterval(() => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      try { this._ws.send(JSON.stringify({ type: 'ping' })); } catch (_) {}
    }, ms);
  },

  _scheduleReconnect() {
    const delays = [1, 2, 5, 10, 30];
    const attempt = this._reconnectAttempt || 0;
    const delaySec = delays[Math.min(attempt, delays.length - 1)];
    this._reconnectAttempt = attempt + 1;
    if (this._term) {
      this._term.write(`\r\n\x1b[31m✗ Connection closed\x1b[0m\r\n`);
      this._term.write(`\x1b[33m⟳ Reconnecting in ${delaySec}s…\x1b[0m`);
    }
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (this._term) this._term.write('\r\n\x1b[33m⟳ Reconnecting…\x1b[0m\r\n');
      this._connectWebSocket();
    }, delaySec * 1000);
  },

  _connectWebSocket() {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._clearHeartbeatPing();

    let ws;
    try {
      const wsUrl = this._resolveWsUrl();
      ws = new WebSocket(wsUrl);
      ws.binaryType = 'arraybuffer';
    } catch (e) {
      console.error('Failed to create WebSocket:', e);
      if (this._term) this._term.write(`\x1b[31m✗ Failed to connect: ${e.message}\x1b[0m\r\n`);
      this._scheduleReconnect();
      return;
    }
    this._ws = ws;

    ws.onopen = () => {
      if (ws !== this._ws) return;
      this._connected = true;
      this._reconnectAttempt = 0;
      this._updateStatus(true);
      this._startHeartbeatPing();
    };

    ws.onmessage = (event) => {
      if (ws !== this._ws) return;
      try {
        const data = event.data;
        if (data instanceof ArrayBuffer) {
          const text = new TextDecoder('latin-1').decode(data);
          this._term.write(text);
        } else {
          this._term.write(data);
        }
      } catch (e) {
        console.error('Error handling message:', e);
      }
    };

    ws.onerror = (event) => {
      if (ws !== this._ws) return;
      console.error('WebSocket error:', event);
      if (this._term) this._term.write('\x1b[31m✗ WebSocket error\x1b[0m\r\n');
      this._clearHeartbeatPing();
    };

    ws.onclose = () => {
      if (ws !== this._ws) return;
      this._clearHeartbeatPing();
      this._connected = false;
      this._updateStatus(false);
      this._scheduleReconnect();
    };
  },

  // Override disconnect() to cancel pending reconnect and heartbeat timers
  disconnect() {
    this._clearHeartbeatPing();
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._waitingForReconnect = false;
    if (this._ws) {
      this._ws.close();
      this._ws = null;
    }
  },

  // ── Terminal Input Handler ────────────────────────────────────────────────

  _handleTerminalInput(data) {
    if (!this._connected || !this._ws) return;
    try {
      this._ws.send(data);
    } catch (e) {
      console.error('Failed to send data:', e);
      if (this._term) this._term.write(`\x1b[31m✗ Failed to send input: ${e.message}\x1b[0m\r\n`);
    }
  },

  // ── Status Updates ────────────────────────────────────────────────────────

  _updateStatus(isConnected) {
    const dot  = this._q('statusDot');
    const text = this._q('statusText');
    const led  = this._q('ledIndicator');
    const info = this._q('connectionInfo');

    if (dot)  dot.classList.toggle('connected', isConnected);
    if (text) text.textContent = isConnected ? 'Connected' : 'Disconnected';
    if (led)  led.classList.toggle('on', isConnected);
    if (info) info.textContent = `${this._settings.cols}×${this._settings.rows}`;
  }
});

