/**
 * ProvideHijack - Embeddable terminal hijack control widget.
 *
 * Connects to the TermHub browser WebSocket endpoint (/ws/browser/{workerId}/term)
 * and provides live terminal viewing plus hijack controls (pause/step/release).
 *
 * Usage:
 *   const w = new ProvideHijack(containerEl, { workerId: 'myworker' });
 *   w.connect();    // called automatically on construction
 *   w.disconnect(); // close WS
 *   w.dispose();    // tear down entirely
 */
'use strict';

// ── Module-level guards ───────────────────────────────────────────────────────
let _hijackCssInjected = false;
let _hijackInstanceCount = 0;
// Capture script element synchronously (available only during initial parse)
const _hijackScriptEl = typeof document !== 'undefined' ? document.currentScript : null;


// ── CSS injection ─────────────────────────────────────────────────────────────
function _injectHijackCSS() {
  if (_hijackCssInjected) return;
  _hijackCssInjected = true;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = (_hijackScriptEl && _hijackScriptEl.src)
    ? _hijackScriptEl.src.replace(/[^/]*$/, '') + 'hijack.css'
    : 'hijack.css';
  document.head.appendChild(link);
}

// ── ProvideHijack class ─────────────────────────────────────────────────────────
class ProvideHijack {
  /**
   * Create an embeddable hijack control widget.
   *
   * @param {HTMLElement} container - Element to mount the widget into.
   * @param {object} [config={}] - Configuration options.
   * @param {string} [config.wsUrl] - Full WS URL. If omitted, auto-built from workerId.
   * @param {string} [config.workerId] - Worker ID (used in auto URL and display title).
   * @param {string} [config.wsPathPrefix='/ws/browser'] - Path prefix for auto URL construction.
   * @param {string|null} [config.title] - Override toolbar title text.
   * @param {boolean} [config.showInput=true] - Show text-input bar when hijacked.
   * @param {boolean} [config.showAnalysis=true] - Show collapsible analysis panel.
   * @param {number} [config.heartbeatInterval=5000] - Heartbeat interval (ms) when hijacked.
   * @param {boolean} [config.mobileKeys=true] - Show collapsible special-key toolbar when hijacked.
   */
  constructor(container, config = {}) {
    this._container = container;
    this._config = {
      wsPathPrefix: '/ws/browser',
      showInput: true,
      showAnalysis: true,
      heartbeatInterval: 5000,
      mobileKeys: true,
      ...config,
    };
    this._uid = ++_hijackInstanceCount;

    // Instance state
    this._ws = null;
    this._term = null;
    this._fitAddon = null;
    this._ro = null;
    this._heartbeatTimer = null;
    this._reconnectTimer = null;
    this._reconnectAttempt = 0;
    this._hijacked = false;
    this._hijackedByMe = false;
    this._workerOnline = false;
    this._inputMode = 'hijack'; // "hijack" | "open"
    this._mobileKeysVisible = false;
    this._root = null;

    _injectHijackCSS();
    this._buildDOM();
    this.connect();
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  /** Open the WebSocket connection. Called automatically on construction. */
  connect() {
    this._connectWs();
  }

  /** Close the WebSocket connection. */
  disconnect() {
    this._clearHeartbeat();
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._ws) {
      try { this._ws.close(); } catch (_) {}
      this._ws = null;
    }
  }

  /** Tear down entirely: xterm, WebSocket, ResizeObserver, and DOM. */
  dispose() {
    this.disconnect(); // handles _ro, _heartbeatTimer, _ws, _reconnectTimer
    if (this._term) { this._term.dispose(); this._term = null; }
    this._fitAddon = null;
    if (this._root && this._root.parentNode) {
      this._root.parentNode.removeChild(this._root);
    }
    this._root = null;
  }

  // ── Internal helpers ────────────────────────────────────────────────────────

  /** Query by ID within this instance's root (IDs are prefixed with h-{uid}-). */
  _q(id) {
    return this._root.querySelector('#h-' + this._uid + '-' + id);
  }

  /** Escape HTML special characters to prevent XSS when interpolating into innerHTML. */
  _escHtml(s) {
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  _resolveWsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = this._config.wsUrl;
    if (url) {
      if (url.startsWith('/')) return `${proto}//${location.host}${url}`;
      return url; // already absolute ws:// or wss://
    }
    const workerId = encodeURIComponent(this._config.workerId || 'default');
    const prefix = this._config.wsPathPrefix || '/ws/browser';
    return `${proto}//${location.host}${prefix}/${workerId}/term`;
  }

  // ── DOM Construction ────────────────────────────────────────────────────────

  _buildDOM() {
    const p = (id) => `h-${this._uid}-${id}`; // ID prefix helper
    const workerId = this._config.workerId || '';
    const title = this._config.title || (workerId ? `Terminal: ${workerId}` : 'Terminal');
    const showAnalysis = this._config.showAnalysis !== false;

    const root = document.createElement('div');
    root.className = 'provide-hijack';
    root.innerHTML = `
      <div class="hijack-toolbar">
        <span class="hijack-title">${this._escHtml(title)}</span>
        <span class="hijack-status">
          <span class="hijack-status-dot" id="${p('dot')}"></span>
          <span id="${p('statustext')}">Connecting…</span>
        </span>
        <div class="hijack-controls">
          <button class="hbtn primary" id="${p('hijack')}" disabled>Hijack</button>
          <button class="hbtn" id="${p('step')}" disabled>Step</button>
          <button class="hbtn danger" id="${p('release')}" disabled>Release</button>
          <button class="hbtn" id="${p('resync')}" disabled title="Request snapshot">⟳ Resync</button>
          <button class="hbtn" id="${p('analyze')}" disabled>Analyze</button>
          <button class="hbtn" id="${p('kbdtoggle')}" title="Mobile key toolbar">⌨</button>
        </div>
        <span class="hijack-prompt" id="${p('prompt')}" title="Current prompt ID"></span>
      </div>
      <div class="hijack-terminal" id="${p('terminal')}"></div>
      <div class="hijack-input-row" id="${p('inputrow')}">
        <input class="hijack-input-field" id="${p('inputfield')}"
          placeholder="Send keys… (Enter to send, e.g. \\r for Return)"
          autocomplete="off" spellcheck="false">
        <button class="hijack-input-send" id="${p('inputsend')}">Send</button>
      </div>
      <div class="mobile-keys" id="${p('mobilekeys')}"></div>
      ${showAnalysis ? `
      <details class="hijack-analysis" id="${p('analysis')}">
        <summary>Analysis</summary>
        <pre id="${p('analysistext')}"></pre>
      </details>` : ''}
    `;

    this._root = root;
    this._container.appendChild(root);
    this._bindEvents();
  }

  // ── xterm ─────────────────────────────────────────────────────────────────

  _ensureTerm() {
    if (this._term) return this._term;
    if (!window.Terminal) throw new Error('xterm.js not loaded');

    const termDiv = this._q('terminal');
    this._term = new window.Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: "'Fira Code', 'Cascadia Code', 'Consolas', monospace",
      fontSize: 13,
      theme: { background: '#0b0f14' },
      allowTransparency: true,
    });
    this._term.open(termDiv);
    this._term.focus();

    if (window.FitAddon) {
      this._fitAddon = new FitAddon.FitAddon();
      this._term.loadAddon(this._fitAddon);
      requestAnimationFrame(() => { try { this._fitAddon.fit(); } catch (_) {} });
      this._ro = new ResizeObserver(() => { try { this._fitAddon.fit(); } catch (_) {} });
      this._ro.observe(termDiv);
    }

    // Forward keyboard input to WS when hijacked or in open mode
    this._term.onData((data) => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (this._inputMode !== 'open' && !this._hijackedByMe) return;
      this._wsSend({ type: 'input', data });
    });

    return this._term;
  }

  // ── WebSocket ─────────────────────────────────────────────────────────────

  _wsSend(obj) {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify(obj));
    }
  }

  _clearHeartbeat() {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  _startHeartbeat() {
    this._clearHeartbeat();
    this._heartbeatTimer = setInterval(() => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (!this._hijackedByMe) return;
      this._wsSend({ type: 'heartbeat' });
    }, this._config.heartbeatInterval || 5000);
  }

  _connectWs() {
    if (this._ws) {
      try { this._ws.close(); } catch (_) {}
      this._ws = null;
    }
    // Do NOT reset _hijacked/_hijackedByMe here: the server will confirm the
    // actual state via 'hello'/'hijack_state' once the socket opens.  Resetting
    // eagerly would briefly re-enable the Hijack button even when another client
    // holds the lock, and could prompt a spurious hijack_request click.
    // (State is correctly reset to false in ws.onclose when the connection drops.)

    let ws;
    try {
      ws = new WebSocket(this._resolveWsUrl());
    } catch (e) {
      this._setStatus('bad', `Failed: ${e.message}`);
      return;
    }
    this._ws = ws;

    ws.onopen = () => {
      if (ws !== this._ws) return; // stale handler: a newer socket already replaced this one
      this._reconnectAttempt = 0;
      this._setStatus('live', 'Connected (watching)');
      this._updateButtons();
      this._wsSend({ type: 'snapshot_req' });
      this._startHeartbeat();
    };

    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (!msg || !msg.type) return;
      this._handleMessage(msg);
    };

    ws.onclose = () => {
      if (ws !== this._ws) return; // stale handler from a replaced socket
      this._clearHeartbeat();
      this._hijacked = false;
      this._hijackedByMe = false;
      this._workerOnline = false;
      this._inputMode = 'hijack';
      this._updateStatus();
      this._updateButtons();
      this._ws = null;
      this._scheduleReconnect();
    };

    ws.onerror = () => {
      try { ws.close(); } catch (_) {}
    };
  }

  _scheduleReconnect() {
    if (this._reconnectTimer) return; // already scheduled
    const delays = [1, 2, 5, 10, 30];
    const attempt = this._reconnectAttempt;
    const delaySec = delays[Math.min(attempt, delays.length - 1)];
    this._reconnectAttempt = attempt + 1;
    this._setStatus('bad', `Reconnecting in ${delaySec}s…`);
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._connectWs();
    }, delaySec * 1000);
  }

  _buildMobileKeys() {
    const container = this._q('mobilekeys');
    if (!container) return;
    const keys = [
      { label: 'ESC', data: '\x1b' },
      { label: '↑',   data: '\x1b[A' },
      { label: '↓',   data: '\x1b[B' },
      { label: '→',   data: '\x1b[C' },
      { label: '←',   data: '\x1b[D' },
      { label: 'Tab', data: '\t' },
      { label: '^C',  data: '\x03' },
      { label: '^D',  data: '\x04' },
      { label: '^Z',  data: '\x1a' },
    ];
    for (const { label, data } of keys) {
      const btn = document.createElement('button');
      btn.className = 'mkey';
      btn.textContent = label;
      btn.addEventListener('click', () => {
        if (this._inputMode !== 'open' && !this._hijackedByMe) return;
        this._wsSend({ type: 'input', data });
      });
      container.appendChild(btn);
    }
  }

  // ── Message dispatch ──────────────────────────────────────────────────────

  _handleMessage(msg) {
    switch (msg.type) {

      case 'term':
        this._workerOnline = true;
        if (msg.data) {
          try { this._ensureTerm().write(msg.data); } catch (_) {}
        }
        break;

      case 'snapshot': {
        this._workerOnline = true;
        const promptId = msg.prompt_detected && msg.prompt_detected.prompt_id;
        this._setPromptId(promptId || '');
        try {
          const t = this._ensureTerm();
          t.reset();
          t.write('\u001b[2J\u001b[H');
          t.write((msg.screen || '').replace(/\n/g, '\r\n'));
        } catch (_) {}
        break;
      }

      case 'analysis': {
        const pre = this._q('analysistext');
        if (pre) {
          pre.textContent = msg.formatted || '(no analysis)';
          const details = this._q('analysis');
          if (details) details.open = true;
        }
        break;
      }

      case 'hello':
        // {type, worker_id, can_hijack, hijacked, hijacked_by_me, input_mode}
        this._hijacked = !!msg.hijacked;
        this._hijackedByMe = !!msg.hijacked_by_me;
        this._workerOnline = !!msg.worker_online;
        if (msg.input_mode) this._inputMode = msg.input_mode;
        this._updateStatus();
        this._updateButtons();
        break;

      case 'worker_connected':
        this._workerOnline = true;
        this._updateStatus();
        this._updateButtons();
        break;

      case 'hijack_state':
        // {type, hijacked, owner: "me"|"other"|null, lease_expires_at, input_mode}
        this._hijacked = !!msg.hijacked;
        this._hijackedByMe = msg.owner === 'me';
        if (msg.input_mode) this._inputMode = msg.input_mode;
        // Keep the heartbeat interval in sync with ownership.
        if (this._hijackedByMe) {
          this._startHeartbeat();
        } else {
          this._clearHeartbeat();
        }
        this._updateStatus();
        this._updateButtons();
        break;

      case 'worker_disconnected':
        this._workerOnline = false;
        this._hijacked = false;
        this._hijackedByMe = false;
        this._clearHeartbeat();
        this._updateStatus();
        this._updateButtons();
        break;

      case 'input_mode_changed':
        if (msg.input_mode) this._inputMode = msg.input_mode;
        this._updateStatus();
        this._updateButtons();
        break;

      case 'heartbeat_ack':
        break; // lease refreshed — no visible change needed

      case 'error':
        this._setStatus('bad', `Error: ${msg.message || 'unknown'}`);
        break;
    }
  }

  // ── UI State ──────────────────────────────────────────────────────────────

  _setStatus(level, text) {
    const dot = this._q('dot');
    const txt = this._q('statustext');
    if (dot) {
      dot.className = 'hijack-status-dot ' + level;
    }
    if (txt) txt.textContent = text;
  }

  _updateStatus() {
    const connected = !!(this._ws && this._ws.readyState === WebSocket.OPEN);
    if (!connected) {
      this._setStatus('bad', 'Disconnected');
    } else if (this._hijackedByMe) {
      this._setStatus('warn', 'Hijacked (you)');
    } else if (this._hijacked) {
      this._setStatus('bad', 'Hijacked (other)');
    } else if (!this._workerOnline) {
      this._setStatus('bad', 'Worker offline');
    } else if (this._inputMode === 'open') {
      this._setStatus('live', 'Connected (shared)');
    } else {
      this._setStatus('live', 'Connected (watching)');
    }

    // Show/hide text-input row based on whether we can send input
    const canInput = this._hijackedByMe || this._inputMode === 'open';
    if (this._config.showInput !== false) {
      const row = this._q('inputrow');
      if (row) row.classList.toggle('visible', connected && canInput);
    }

    // Show/hide mobile-keys row
    if (this._config.mobileKeys !== false) {
      const mkRow = this._q('mobilekeys');
      if (mkRow) mkRow.classList.toggle('visible', connected && canInput && this._mobileKeysVisible);
    }
  }

  _updateButtons() {
    const connected = !!(this._ws && this._ws.readyState === WebSocket.OPEN);
    const hijackBtn  = this._q('hijack');
    const stepBtn    = this._q('step');
    const releaseBtn = this._q('release');
    const resyncBtn  = this._q('resync');
    const analyzeBtn = this._q('analyze');

    if (!connected) {
      [hijackBtn, stepBtn, releaseBtn, resyncBtn, analyzeBtn]
        .forEach(b => { if (b) b.disabled = true; });
      return;
    }
    const isOpen = this._inputMode === 'open';
    if (hijackBtn)  hijackBtn.disabled  = isOpen || this._hijacked || !this._workerOnline;
    if (stepBtn)    stepBtn.disabled    = isOpen || !this._hijackedByMe;
    if (releaseBtn) releaseBtn.disabled = isOpen || !this._hijackedByMe;
    if (resyncBtn)  resyncBtn.disabled  = !this._workerOnline;
    if (analyzeBtn) analyzeBtn.disabled = isOpen || !this._hijackedByMe;
    // Hide hijack controls entirely in open mode
    if (hijackBtn)  hijackBtn.style.display  = isOpen ? 'none' : '';
    if (stepBtn)    stepBtn.style.display    = isOpen ? 'none' : '';
    if (releaseBtn) releaseBtn.style.display = isOpen ? 'none' : '';
  }

  _setPromptId(id) {
    const el = this._q('prompt');
    if (el) el.textContent = id ? `prompt: ${id}` : '';
  }

  // ── Event Binding ─────────────────────────────────────────────────────────

  _bindEvents() {
    this._q('hijack').addEventListener('click', () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      this._wsSend({ type: 'hijack_request' });
    });

    this._q('step').addEventListener('click', () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (!this._hijackedByMe) return;
      this._wsSend({ type: 'hijack_step' });
      // Request snapshot + analysis shortly after the worker acts
      for (const ms of [250, 1000]) {
        setTimeout(() => {
          if (this._ws && this._ws.readyState === WebSocket.OPEN)
            this._wsSend({ type: 'snapshot_req' });
        }, ms);
      }
      for (const ms of [450, 1200]) {
        setTimeout(() => {
          if (this._ws && this._ws.readyState === WebSocket.OPEN)
            this._wsSend({ type: 'analyze_req' });
        }, ms);
      }
    });

    this._q('release').addEventListener('click', () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      this._wsSend({ type: 'hijack_release' });
    });

    this._q('resync').addEventListener('click', () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      this._wsSend({ type: 'snapshot_req' });
    });

    this._q('analyze').addEventListener('click', () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (!this._hijackedByMe) return;
      this._wsSend({ type: 'analyze_req' });
    });

    // Mobile key toolbar toggle
    if (this._config.mobileKeys !== false) {
      this._buildMobileKeys();
      const kbdToggle = this._q('kbdtoggle');
      if (kbdToggle) {
        kbdToggle.addEventListener('click', () => {
          this._mobileKeysVisible = !this._mobileKeysVisible;
          this._updateStatus();
        });
      }
    }

    // Text input send — for pasting strings or escape sequences when hijacked
    const inputField = this._q('inputfield');
    const inputSend  = this._q('inputsend');
    if (inputField) {
      const doSend = () => {
        const raw = inputField.value;
        if (!raw || (this._inputMode !== 'open' && !this._hijackedByMe)) return;
        if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
        // Unescape \\r → \r, \\n → \n, \\t → \t, \\e → ESC
        const data = raw
          .replace(/\\r/g, '\r')
          .replace(/\\n/g, '\n')
          .replace(/\\t/g, '\t')
          .replace(/\\e/g, '\x1b');
        this._wsSend({ type: 'input', data });
        inputField.value = '';
        try { this._ensureTerm().focus(); } catch (_) {}
      };
      inputField.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); doSend(); }
      });
      if (inputSend) inputSend.addEventListener('click', doSend);
    }
  }
}

// ── Global exposure for CDN / script-tag use ──────────────────────────────────
if (typeof window !== 'undefined') window.ProvideHijack = ProvideHijack;
