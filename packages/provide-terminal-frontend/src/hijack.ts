//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

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

import {
  _RECONNECT_ANIM_FRAMES,
  ControlChannelDecoder,
  encodeWsFrame,
  type FitAddonInstance,
  type HijackConfig,
  type ResolvedConfig,
  type XTerminal,
} from "./hijack-codec.js";

// ── Module-level guards ───────────────────────────────────────────────────────
let _hijackCssInjected = false;
let _hijackInstanceCount = 0;
// Resolve CSS base URL via import.meta.url (works for ES modules; document.currentScript is null for modules)
const _hijackCssBase = new URL("./", import.meta.url).href;

// ── CSS injection ─────────────────────────────────────────────────────────────
function _injectHijackCSS(): void {
  if (_hijackCssInjected) return;
  _hijackCssInjected = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = `${_hijackCssBase}hijack.css`;
  document.head.appendChild(link);
}

// ── ProvideHijack class ─────────────────────────────────────────────────────────
export class ProvideHijack {
  // Private state
  private readonly _container: HTMLElement;
  private readonly _config: ResolvedConfig;
  private readonly _uid: number;
  private readonly _workerId: string;
  private readonly _wsDecoder: ControlChannelDecoder;

  private _ws: WebSocket | null = null;
  private _term: XTerminal | null = null;
  private _fitAddon: FitAddonInstance | null = null;
  private _ro: ResizeObserver | null = null;
  private _heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectAnimTimer: ReturnType<typeof setInterval> | null = null;
  private _reconnectAttempt = 0;
  private _hijacked = false;
  private _hijackedByMe = false;
  private _canHijack = false;
  private _workerOnline = false;
  private _inputMode = "hijack"; // "hijack" | "open"
  private _hijackControl = "ws"; // "ws" | "rest"
  private _hijackStepSupported = true;
  private _restHijackId: string | null = null;
  private _resumeToken: string | null = null;
  private _mobileKeysVisible = false;
  private _wakingTimer: ReturnType<typeof setTimeout> | null = null;
  private _wakingTimedOut = false;
  private _activityFlashTimer: ReturnType<typeof setTimeout> | null = null;
  private _statusDotElement: HTMLElement | null = null;
  private _root: HTMLElement | null = null;

  /**
   * Create an embeddable hijack control widget.
   *
   * @param container - Element to mount the widget into.
   * @param config - Configuration options.
   */
  constructor(container: HTMLElement, config: HijackConfig = {}) {
    this._container = container;
    this._config = {
      wsUrl: config.wsUrl,
      workerId: config.workerId,
      wsPathPrefix: config.wsPathPrefix ?? "/ws/browser",
      authToken: config.authToken,
      title: config.title,
      showInput: config.showInput ?? true,
      showAnalysis: config.showAnalysis ?? true,
      heartbeatInterval: config.heartbeatInterval ?? 5000,
      mobileKeys: config.mobileKeys ?? true,
      role: config.role,
      onResize: config.onResize,
    };
    this._uid = ++_hijackInstanceCount;
    this._workerId = config.workerId ?? "default";
    this._wsDecoder = new ControlChannelDecoder();

    _injectHijackCSS();
    this._buildDOM();
    this.connect();
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  /** Open the WebSocket connection. Called automatically on construction. */
  connect(): void {
    this._connectWs();
  }

  /** Close the WebSocket connection. */
  disconnect(): void {
    this._clearHeartbeat();
    if (this._ro) {
      this._ro.disconnect();
      this._ro = null;
    }
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._ws) {
      try {
        this._ws.close();
      } catch (_) {}
      this._ws = null;
    }
  }

  /** Tear down entirely: xterm, WebSocket, ResizeObserver, and DOM. */
  dispose(): void {
    this.disconnect(); // handles _ro, _heartbeatTimer, _ws, _reconnectTimer
    this._stopReconnectAnim();
    if (this._activityFlashTimer) {
      clearTimeout(this._activityFlashTimer);
      this._activityFlashTimer = null;
    }
    if (this._term) {
      this._term.dispose();
      this._term = null;
    }
    this._fitAddon = null;
    this._statusDotElement = null;
    if (this._root?.parentNode) {
      this._root.parentNode.removeChild(this._root);
    }
    this._root = null;
  }

  // ── Internal helpers ────────────────────────────────────────────────────────

  /** Query by ID within this instance's root (IDs are prefixed with h-{uid}-). */
  private _q(id: string): HTMLElement | null {
    return this._root?.querySelector<HTMLElement>(`#h-${this._uid}-${id}`) ?? null;
  }

  /** Escape HTML special characters to prevent XSS when interpolating into innerHTML. */
  private _escHtml(s: unknown): string {
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  private _saveResumeToken(token: string): void {
    try {
      sessionStorage.setItem(`uterm_resume_${this._workerId}`, token);
    } catch (_) {}
  }

  private _loadResumeToken(): string | null {
    try {
      return sessionStorage.getItem(`uterm_resume_${this._workerId}`);
    } catch (_) {
      return null;
    }
  }

  private _resolveWsUrl(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = this._config.wsUrl;
    const authToken = this._config.authToken;
    const appendToken = (value: string): string => {
      if (!authToken) return value;
      const parsed = new URL(value, `${proto}//${location.host}`);
      parsed.searchParams.set("token", authToken);
      return parsed.toString();
    };
    if (url) {
      if (url.startsWith("/")) return appendToken(`${proto}//${location.host}${url}`);
      return appendToken(url); // already absolute ws:// or wss://
    }
    const workerId = encodeURIComponent(this._config.workerId ?? "default");
    const prefix = this._config.wsPathPrefix;
    return appendToken(`${proto}//${location.host}${prefix}/${workerId}/term`);
  }

  private _resolveHijackApiBase(): string {
    const workerId = encodeURIComponent(this._config.workerId ?? "default");
    return `/worker/${workerId}/hijack`;
  }

  private _withAuthToken(path: string): string {
    if (!this._config.authToken) {
      return path;
    }
    const separator = path.includes("?") ? "&" : "?";
    return `${path}${separator}token=${encodeURIComponent(this._config.authToken)}`;
  }

  private async _restHijack(
    action: "acquire" | "heartbeat" | "release" | "step",
    payload: Record<string, unknown> = {},
  ): Promise<Record<string, unknown> | null> {
    const headers = { "content-type": "application/json" };
    const base = this._resolveHijackApiBase();
    let path = "";
    if (action === "acquire") {
      path = `${base}/acquire`;
    } else {
      if (!this._restHijackId) return null;
      path = `${base}/${encodeURIComponent(this._restHijackId)}/${action}`;
    }
    const resp = await fetch(this._withAuthToken(path), {
      method: "POST",
      credentials: "include",
      headers,
      body: JSON.stringify(payload),
    });
    if (!resp.ok) return null;
    return (await resp.json()) as Record<string, unknown>;
  }

  // ── DOM Construction ────────────────────────────────────────────────────────

  private _buildDOM(): void {
    const p = (id: string) => `h-${this._uid}-${id}`; // ID prefix helper
    const workerId = this._config.workerId ?? "";
    const title = this._config.title ?? (workerId ? workerId : "Terminal");
    const showAnalysis = this._config.showAnalysis;

    const root = document.createElement("div");
    root.className = "provide-hijack";
    root.innerHTML = `
      <div class="hijack-toolbar">
        <span class="hijack-title">${this._escHtml(title)}</span>
        <span class="hijack-status">
          <span class="hijack-status-dot" id="${p("dot")}"></span>
          <span id="${p("statustext")}">Connecting…</span>
        </span>
        <div class="hijack-controls">
          <button class="hbtn primary" id="${p("hijack")}" disabled title="Take exclusive control">Hijack</button>
          <button class="hbtn" id="${p("step")}" disabled title="Send one step, then pause">Step</button>
          <button class="hbtn danger" id="${p("release")}" disabled title="Release hijack control">Release</button>
          <button class="hbtn" id="${p("resync")}" disabled title="Request full screen snapshot">⟳ Resync</button>
          <button class="hbtn" id="${p("analyze")}" disabled title="AI-readable screen description">Analyze</button>
          <button class="hbtn" id="${p("kbdtoggle")}" title="Toggle mobile key toolbar">⌨</button>
        </div>
        <span class="hijack-prompt" id="${p("prompt")}" title="Current prompt ID"></span>
      </div>
      <div class="hijack-terminal" id="${p("terminal")}"></div>
      <div class="hijack-input-row" id="${p("inputrow")}">
        <input class="hijack-input-field" id="${p("inputfield")}"
          placeholder="Send keys… (Enter to send, e.g. \\r for Return)"
          autocomplete="off" spellcheck="false">
        <button class="hijack-input-send" id="${p("inputsend")}">Send</button>
      </div>
      <div class="mobile-keys" id="${p("mobilekeys")}"></div>
      ${
        showAnalysis
          ? `
      <details class="hijack-analysis" id="${p("analysis")}">
        <summary>Analysis</summary>
        <pre id="${p("analysistext")}"></pre>
      </details>`
          : ""
      }
    `;

    this._root = root;
    this._container.appendChild(root);
    this._bindEvents();
  }

  // ── xterm ─────────────────────────────────────────────────────────────────

  private _ensureTerm(): XTerminal {
    if (this._term) return this._term;
    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const terminalCtor = (window as any).Terminal as (new (opts: Record<string, unknown>) => XTerminal) | undefined;
    if (!terminalCtor) throw new Error("xterm.js not loaded");

    const termDiv = this._q("terminal");
    if (!termDiv) throw new Error("terminal container not found");
    this._term = new terminalCtor({
      convertEol: false,
      cursorBlink: true,
      fontFamily: "'Fira Code', 'Cascadia Code', 'Consolas', monospace",
      fontSize: 13,
      theme: { background: "#0b0f14" },
      allowTransparency: true,
      scrollback: 10000,
      scrollOnUserInput: true,
      overviewRulerWidth: 15,
    });
    this._term.open(termDiv);
    this._term.focus();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const fitAddonGlobal = ((window as any).FitAddon ?? (globalThis as any).FitAddon) as
      | { FitAddon: new () => FitAddonInstance }
      | undefined;
    if (fitAddonGlobal) {
      this._fitAddon = new fitAddonGlobal.FitAddon();
      this._term.loadAddon(this._fitAddon);
      requestAnimationFrame(() => {
        try {
          this._fitAddon?.fit();
          if (this._term && this._term.cols > 0 && this._term.rows > 0) {
            this._config.onResize?.(this._term.cols, this._term.rows);
          }
        } catch (_) {}
      });
      this._ro = new ResizeObserver(() => {
        try {
          this._fitAddon?.fit();
          if (this._term && this._term.cols > 0 && this._term.rows > 0) {
            this._config.onResize?.(this._term.cols, this._term.rows);
          }
        } catch (_) {}
      });
      this._ro.observe(termDiv);
    }

    // Load WebLinksAddon if available
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const webLinksAddonGlobal = (window as any).WebLinksAddon ?? (globalThis as any).WebLinksAddon;
    if (webLinksAddonGlobal) {
      try {
        this._term.loadAddon(new webLinksAddonGlobal.WebLinksAddon());
      } catch (_) {}
    }

    // Forward keyboard input to WS when hijacked or in open mode
    this._term.onData((data: string) => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
        this._nudgeReconnect();
        return;
      }
      if (this._inputMode !== "open" && !this._hijackedByMe) return;
      // Show activity indicator (no local echo — let server echo drive the display)
      this._showActivityIndicator();
      // Send to server asynchronously
      this._wsSend({ type: "input", data });
    });

    return this._term;
  }

  // ── WebSocket ─────────────────────────────────────────────────────────────

  private _wsSend(obj: Record<string, unknown>): void {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(encodeWsFrame(obj));
    }
  }

  private _clearHeartbeat(): void {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  private _startHeartbeat(): void {
    this._clearHeartbeat();
    this._heartbeatTimer = setInterval(() => {
      if (!this._hijackedByMe) return;
      if (this._hijackControl === "rest") {
        this._restHijack("heartbeat", { lease_s: 60 }).catch(() => {});
        return;
      }
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      this._wsSend({ type: "heartbeat" });
    }, this._config.heartbeatInterval);
  }

  private _connectWs(): void {
    if (this._ws) {
      try {
        this._ws.close();
      } catch (_) {}
      this._ws = null;
    }
    // Do NOT reset _hijacked/_hijackedByMe here: the server will confirm the
    // actual state via 'hello'/'hijack_state' once the socket opens.  Resetting
    // eagerly would briefly re-enable the Hijack button even when another client
    // holds the lock, and could prompt a spurious hijack_request click.
    // (State is correctly reset to false in ws.onclose when the connection drops.)

    let ws: WebSocket;
    try {
      ws = new WebSocket(this._resolveWsUrl());
    } catch (e) {
      const err = e instanceof Error ? e : new Error(String(e));
      this._setStatus("bad", `Failed: ${err.message}`);
      return;
    }
    this._ws = ws;
    this._wsDecoder.reset();

    ws.onopen = () => {
      if (ws !== this._ws) return; // stale handler: a newer socket already replaced this one
      this._stopReconnectAnim();
      this._reconnectAttempt = 0;
      this._wakingTimedOut = false;
      if (this._wakingTimer) clearTimeout(this._wakingTimer);
      this._wakingTimer = setTimeout(() => {
        this._wakingTimer = null;
        if (!this._workerOnline && this._ws?.readyState === WebSocket.OPEN) {
          this._wakingTimedOut = true;
          this._updateStatus();
        }
      }, 10_000);
      this._setStatus("warn", "Waking…");
      this._updateButtons();
      // Attempt session resumption if we have a stored token
      const storedToken = this._resumeToken ?? this._loadResumeToken();
      if (storedToken) {
        this._wsSend({ type: "resume", token: storedToken });
      }
      this._wsSend({ type: "snapshot_req" });
      this._startHeartbeat();
    };

    ws.onmessage = (e: MessageEvent) => {
      try {
        const frames = this._wsDecoder.feed(typeof e.data === "string" ? (e.data as string) : String(e.data));
        for (const frame of frames) {
          const msg: Record<string, unknown> =
            frame.type === "data" ? { type: "term", data: frame.data } : frame.control;
          if (msg.type) {
            this._handleMessage(msg);
          }
        }
      } catch (_) {
        this._setStatus("bad", "Protocol error");
        try {
          ws.close();
        } catch (_) {}
        return;
      }
    };

    ws.onclose = () => {
      if (ws !== this._ws) return; // stale handler from a replaced socket
      this._clearHeartbeat();
      if (this._wakingTimer) {
        clearTimeout(this._wakingTimer);
        this._wakingTimer = null;
      }
      this._wakingTimedOut = false;
      this._hijacked = false;
      this._hijackedByMe = false;
      this._canHijack = false;
      this._workerOnline = false;
      this._inputMode = "hijack";
      this._hijackControl = "ws";
      this._hijackStepSupported = true;
      this._restHijackId = null;
      // Do NOT clear _resumeToken — needed for session resumption on reconnect
      this._updateStatus();
      this._updateButtons();
      this._ws = null;
      this._scheduleReconnect();
    };

    ws.onerror = () => {
      try {
        ws.close();
      } catch (_) {}
    };
  }

  private _scheduleReconnect(): void {
    if (this._reconnectTimer) return; // already scheduled
    const delays = [1, 2, 5, 10, 30] as const;
    const attempt = this._reconnectAttempt;
    const delaySec = delays[Math.min(attempt, delays.length - 1)] ?? 30;
    this._reconnectAttempt = attempt + 1;
    this._setStatus("bad", `Reconnecting in ${delaySec}s…`);
    this._startReconnectAnim();
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._connectWs();
    }, delaySec * 1000);
  }

  /** Cancel any pending backoff timer and reconnect immediately. */
  private _nudgeReconnect(): void {
    // Already actively connecting — don't pile on.
    if (this._ws && this._ws.readyState === WebSocket.CONNECTING) return;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
      this._connectWs();
    }
  }

  /** Start a braille spinner rendered below the cursor via ANSI save/restore. */
  private _startReconnectAnim(): void {
    if (this._reconnectAnimTimer || !this._term) return;
    let i = 0;
    this._reconnectAnimTimer = setInterval(() => {
      if (!this._term) return;
      const ch = _RECONNECT_ANIM_FRAMES[i % _RECONNECT_ANIM_FRAMES.length] ?? "⠋";
      i++;
      try {
        this._term.write(`\x1b7\x1b[B\x1b[G\x1b[2;36m${ch}\x1b[0m\x1b8`);
      } catch (_) {}
    }, 80);
  }

  /** Stop the spinner and erase the character it left behind. */
  private _stopReconnectAnim(): void {
    if (!this._reconnectAnimTimer) return;
    clearInterval(this._reconnectAnimTimer);
    this._reconnectAnimTimer = null;
    if (this._term) {
      try {
        this._term.write("\x1b7\x1b[B\x1b[G \x1b8");
      } catch (_) {}
    }
  }

  /** Show activity indicator with the configured style (reuses DOM reference and timeout). */
  private _showActivityIndicator(): void {
    // Cache dot element on first use
    if (!this._statusDotElement) {
      this._statusDotElement = this._q("dot");
    }
    const dot = this._statusDotElement;
    if (!dot) return;

    // Clear any pending flash removal
    if (this._activityFlashTimer) clearTimeout(this._activityFlashTimer);

    // Flash the status dot green with glow
    dot.classList.add("activity-flash");

    // Schedule removal of animation class after 200ms (reuse single timeout)
    this._activityFlashTimer = setTimeout(() => {
      dot.classList.remove("activity-flash");
      this._activityFlashTimer = null;
    }, 200);
  }

  // _echoInput removed: server echo drives the terminal display.
  // Local echo caused double-rendering because the dedup (exact string match)
  // fails when the remote host wraps the echo in ANSI sequences.

  private _buildMobileKeys(): void {
    const container = this._q("mobilekeys");
    if (!container) return;
    const keys: Array<{ label: string; data: string }> = [
      { label: "ESC", data: "\x1b" },
      { label: "↑", data: "\x1b[A" },
      { label: "↓", data: "\x1b[B" },
      { label: "→", data: "\x1b[C" },
      { label: "←", data: "\x1b[D" },
      { label: "Tab", data: "\t" },
      { label: "^C", data: "\x03" },
      { label: "^D", data: "\x04" },
      { label: "^Z", data: "\x1a" },
    ];
    for (const { label, data } of keys) {
      const btn = document.createElement("button");
      btn.className = "mkey";
      btn.textContent = label;
      btn.addEventListener("click", () => {
        if (this._inputMode !== "open" && !this._hijackedByMe) return;
        if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
        this._showActivityIndicator();
        this._wsSend({ type: "input", data });
      });
      container.appendChild(btn);
    }
  }

  // ── Message dispatch ──────────────────────────────────────────────────────

  private _handleMessage(msg: Record<string, unknown>): void {
    switch (msg.type as string) {
      case "term":
        this._workerOnline = true;
        if (msg.data) {
          try {
            this._ensureTerm().write(msg.data as string);
          } catch (_) {}
        }
        break;

      case "snapshot": {
        this._stopReconnectAnim();
        this._workerOnline = true;
        const promptDetected = msg.prompt_detected as Record<string, unknown> | undefined;
        const promptId = promptDetected?.prompt_id as string | undefined;
        this._setPromptId(promptId ?? "");
        try {
          const t = this._ensureTerm();
          t.reset();
          t.write("\u001b[2J\u001b[H");
          t.write(((msg.screen as string | undefined) ?? "").replace(/\n/g, "\r\n"));
        } catch (_) {}
        break;
      }

      case "analysis": {
        const pre = this._q("analysistext");
        if (pre) {
          pre.textContent = (msg.formatted as string | undefined) ?? "(no analysis)";
          const details = this._q("analysis");
          if (details) (details as HTMLDetailsElement).open = true;
        }
        break;
      }

      case "hello": {
        // {type, worker_id, can_hijack, hijacked, hijacked_by_me, input_mode, role, resume_token, resumed}
        this._canHijack = !!(msg.can_hijack as boolean | undefined);
        this._hijacked = !!(msg.hijacked as boolean | undefined);
        this._hijackedByMe = !!(msg.hijacked_by_me as boolean | undefined);
        this._workerOnline = !!(msg.worker_online as boolean | undefined);
        const inputMode = msg.input_mode as string | undefined;
        if (inputMode) this._inputMode = inputMode;
        const caps = msg.capabilities as Record<string, unknown> | undefined;
        this._hijackControl =
          (msg.hijack_control as string | undefined) ?? (caps?.hijack_control as string | undefined) ?? "ws";
        const stepSupported =
          (msg.hijack_step_supported as boolean | undefined) ?? (caps?.hijack_step_supported as boolean | undefined);
        this._hijackStepSupported = stepSupported !== false;
        const resumeToken = msg.resume_token as string | undefined;
        if (resumeToken) {
          this._resumeToken = resumeToken;
          this._saveResumeToken(resumeToken);
        }
        this._updateStatus();
        this._updateButtons();
        break;
      }

      case "worker_connected":
        if (this._wakingTimer) {
          clearTimeout(this._wakingTimer);
          this._wakingTimer = null;
        }
        this._wakingTimedOut = false;
        this._workerOnline = true;
        this._updateStatus();
        this._updateButtons();
        break;

      case "hijack_state": {
        // {type, hijacked, owner: "me"|"other"|null, lease_expires_at, input_mode}
        this._hijacked = !!(msg.hijacked as boolean | undefined);
        this._hijackedByMe = (msg.owner as string | undefined) === "me";
        if (!this._hijackedByMe) this._restHijackId = null;
        const hsInputMode = msg.input_mode as string | undefined;
        if (hsInputMode) this._inputMode = hsInputMode;
        // Keep the heartbeat interval in sync with ownership.
        if (this._hijackedByMe) {
          this._startHeartbeat();
        } else {
          this._clearHeartbeat();
        }
        this._updateStatus();
        this._updateButtons();
        break;
      }

      case "worker_disconnected":
        if (this._wakingTimer) {
          clearTimeout(this._wakingTimer);
          this._wakingTimer = null;
        }
        this._wakingTimedOut = true;
        this._workerOnline = false;
        this._hijacked = false;
        this._hijackedByMe = false;
        this._clearHeartbeat();
        this._updateStatus();
        this._updateButtons();
        break;

      case "input_mode_changed": {
        const changedMode = msg.input_mode as string | undefined;
        if (changedMode) this._inputMode = changedMode;
        this._updateStatus();
        this._updateButtons();
        break;
      }

      case "heartbeat_ack":
        break; // lease refreshed — no visible change needed

      case "error":
        this._setStatus("bad", `Error: ${(msg.message as string | undefined) ?? "unknown"}`);
        break;
    }
  }

  // ── UI State ──────────────────────────────────────────────────────────────

  private _setStatus(level: string, text: string): void {
    const dot = this._q("dot");
    const txt = this._q("statustext");
    if (dot) {
      dot.className = `hijack-status-dot ${level}`;
    }
    if (txt) txt.textContent = text;
  }

  private _updateStatus(): void {
    const connected = !!(this._ws && this._ws.readyState === WebSocket.OPEN);
    if (!connected) {
      this._setStatus("bad", "Disconnected"); // red — WS down
    } else if (!this._workerOnline) {
      if (this._wakingTimedOut) {
        this._setStatus("bad", "Offline"); // red — worker never came online
      } else {
        this._setStatus("warn", "Waking…"); // orange — connected but worker not yet online
      }
    } else if (this._hijackedByMe) {
      this._setStatus("live", "Hijacked (you)"); // green — active hijack by this browser
    } else if (this._hijacked) {
      this._setStatus("warn", "Hijacked (other)"); // orange — someone else has control
    } else if (this._inputMode === "open") {
      this._setStatus("live", "Connected (shared)"); // green — active
    } else {
      this._setStatus("live", "Connected (watching)"); // green — active
    }

    // Show/hide text-input row based on whether we can send input
    const canInput = this._hijackedByMe || this._inputMode === "open";
    if (this._config.showInput) {
      const row = this._q("inputrow");
      if (row) row.classList.toggle("visible", connected && canInput);
    }

    // Show/hide mobile-keys row
    if (this._config.mobileKeys) {
      const mkRow = this._q("mobilekeys");
      if (mkRow) mkRow.classList.toggle("visible", connected && canInput && this._mobileKeysVisible);
    }
  }

  private _updateButtons(): void {
    const connected = !!(this._ws && this._ws.readyState === WebSocket.OPEN);
    const hijackBtn = this._q("hijack") as HTMLButtonElement | null;
    const stepBtn = this._q("step") as HTMLButtonElement | null;
    const releaseBtn = this._q("release") as HTMLButtonElement | null;
    const resyncBtn = this._q("resync") as HTMLButtonElement | null;
    const analyzeBtn = this._q("analyze") as HTMLButtonElement | null;

    if (!connected) {
      for (const b of [hijackBtn, stepBtn, releaseBtn, resyncBtn, analyzeBtn]) {
        if (b) b.disabled = true;
      }
      return;
    }
    const isOpen = this._inputMode === "open";
    const hideHijack = isOpen || !this._canHijack;
    if (hijackBtn) hijackBtn.disabled = hideHijack || this._hijacked || !this._workerOnline;
    if (stepBtn) stepBtn.disabled = hideHijack || !this._hijackedByMe || !this._hijackStepSupported;
    if (releaseBtn) releaseBtn.disabled = hideHijack || !this._hijackedByMe;
    if (resyncBtn) resyncBtn.disabled = !this._workerOnline;
    if (analyzeBtn) analyzeBtn.disabled = hideHijack || !this._hijackedByMe;
    // Hide hijack controls for non-admin roles and in open mode
    if (hijackBtn) hijackBtn.style.display = hideHijack ? "none" : "";
    if (stepBtn) stepBtn.style.display = hideHijack ? "none" : "";
    if (releaseBtn) releaseBtn.style.display = hideHijack ? "none" : "";
  }

  private _setPromptId(id: string): void {
    const el = this._q("prompt");
    if (el) el.textContent = id ? `prompt: ${id}` : "";
  }

  // ── Event Binding ─────────────────────────────────────────────────────────

  private _bindEvents(): void {
    this._q("hijack")?.addEventListener("click", () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (this._hijackControl === "rest") {
        this._restHijack("acquire", { owner: "dashboard", lease_s: 60 })
          .then((data) => {
            if (data && typeof data.hijack_id === "string") this._restHijackId = data.hijack_id;
          })
          .finally(() => this._wsSend({ type: "snapshot_req" }));
        return;
      }
      this._wsSend({ type: "hijack_request" });
    });

    this._q("step")?.addEventListener("click", () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (!this._hijackedByMe) return;
      if (this._hijackControl === "rest") {
        this._restHijack("step").catch(() => {});
      } else {
        this._wsSend({ type: "hijack_step" });
      }
      // Request snapshot + analysis shortly after the worker acts
      for (const ms of [250, 1000]) {
        setTimeout(() => {
          if (this._ws && this._ws.readyState === WebSocket.OPEN) this._wsSend({ type: "snapshot_req" });
        }, ms);
      }
      for (const ms of [450, 1200]) {
        setTimeout(() => {
          if (this._ws && this._ws.readyState === WebSocket.OPEN) this._wsSend({ type: "analyze_req" });
        }, ms);
      }
    });

    this._q("release")?.addEventListener("click", () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (this._hijackControl === "rest") {
        this._restHijack("release")
          .then(() => {
            this._restHijackId = null;
          })
          .finally(() => this._wsSend({ type: "snapshot_req" }));
        return;
      }
      this._wsSend({ type: "hijack_release" });
    });

    this._q("resync")?.addEventListener("click", () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      this._wsSend({ type: "snapshot_req" });
    });

    this._q("analyze")?.addEventListener("click", () => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      if (!this._hijackedByMe) return;
      this._wsSend({ type: "analyze_req" });
    });

    // Mobile key toolbar toggle
    if (this._config.mobileKeys) {
      this._buildMobileKeys();
      const kbdToggle = this._q("kbdtoggle");
      if (kbdToggle) {
        kbdToggle.addEventListener("click", () => {
          this._mobileKeysVisible = !this._mobileKeysVisible;
          this._updateStatus();
        });
      }
    }

    // Text input send — for pasting strings or escape sequences when hijacked
    const inputField = this._q("inputfield") as HTMLInputElement | null;
    const inputSend = this._q("inputsend");
    if (inputField) {
      const doSend = () => {
        const raw = inputField.value;
        if (!raw || (this._inputMode !== "open" && !this._hijackedByMe)) return;
        if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
        // Unescape \\r → \r, \\n → \n, \\t → \t, \\e → ESC
        const data = raw.replace(/\\r/g, "\r").replace(/\\n/g, "\n").replace(/\\t/g, "\t").replace(/\\e/g, "\x1b");
        this._showActivityIndicator();
        this._wsSend({ type: "input", data });
        inputField.value = "";
        try {
          this._ensureTerm().focus();
        } catch (_) {}
      };
      inputField.addEventListener("keydown", (e: KeyboardEvent) => {
        if (e.key === "Enter") {
          e.preventDefault();
          doSend();
        }
      });
      if (inputSend) inputSend.addEventListener("click", doSend);
    }
  }
}

// ── Global exposure for CDN / script-tag use ──────────────────────────────────
// biome-ignore lint/suspicious/noExplicitAny: window global access
if (typeof window !== "undefined") (window as any).ProvideHijack = ProvideHijack;
