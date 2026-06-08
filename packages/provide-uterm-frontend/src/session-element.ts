//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { LitElement, html, css, type PropertyValues, unsafeCSS } from "lit";
import { customElement, property, query, state } from "lit/decorators.js";
import type { AnyFrame } from "./generated/frames.js";
import "./approval-prompt-element.js";
import type { ApprovalPromptElement } from "./approval-prompt-element.js";
import {
  ControlChannelDecoder,
  type FitAddonInstance,
  type HijackConfig,
  type ResolvedConfig,
  type XTerminal,
} from "./hijack-codec.js";
import { type HijackHandlers, HijackState } from "./hijack-state.js";
import { MOBILE_KEYS } from "./hijack-ui.js";
import {
  clearHeartbeat,
  connectWs,
  nudgeReconnect,
  restHijack,
  saveResumeToken,
  startHeartbeat,
  stopReconnectAnim,
  wsSend,
} from "./hijack-websocket.js";

// We import the CSS text directly or use a constructed stylesheet.
// Since Vite is used, importing the CSS might just inject it globally if it's imported normally.
// But we can also use Lit's css literal. Let's recreate the styles here from static/hijack.css
// with :host replacing .provide-hijack.

const hijackStyles = css`
  :host {
    width: 100%;
    height: 100%;
    position: relative;
    display: flex;
    flex-direction: column;
    background: #0b0f14;
    color: #e2e8f0;
    font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
    font-size: 13px;
    overflow: hidden;
  }
  * { box-sizing: border-box; }

  /* ── Toolbar ── */
  .hijack-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    background: #141920;
    border-bottom: 1px solid #1e2530;
    flex-shrink: 0;
    flex-wrap: nowrap;
    height: 30px;
  }

  .hijack-title {
    font-family: 'Fira Code', monospace;
    font-size: 11px;
    color: #7a9ab8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
    flex-shrink: 1;
  }

  .hijack-status {
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .hijack-status-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #444;
    flex-shrink: 0;
  }
  .hijack-status-dot.live { background: #22c55e; box-shadow: 0 0 4px #22c55e; animation: live-pulse 2s ease-in-out infinite; }

  @keyframes live-pulse {
    0%, 100% { box-shadow: 0 0 2px #22c55e; }
    50%       { box-shadow: 0 0 7px #22c55e; }
  }
  .hijack-status-dot.warn { background: #f59e0b; box-shadow: 0 0 4px #f59e0b; }
  .hijack-status-dot.bad  { background: #ef4444; box-shadow: 0 0 4px #ef4444; }

  /* ── Activity indicator animations ── */
  .hijack-status-dot.activity-flash {
    background: #22c55e;
    box-shadow: 0 0 8px #22c55e;
    animation: activity-flash 200ms ease-out;
  }

  @keyframes activity-flash {
    0% {
      box-shadow: 0 0 12px #22c55e;
    }
    100% {
      box-shadow: 0 0 2px #22c55e;
    }
  }

  .hijack-controls {
    display: flex;
    gap: 3px;
    align-items: center;
    flex-wrap: nowrap;
    border-left: 1px solid #1e2530;
    margin-left: 4px;
    padding-left: 6px;
  }

  .hbtn {
    padding: 2px 8px;
    border-radius: 4px;
    border: 1px solid #2a3040;
    background: #1e2530;
    color: #ccc;
    font-family: 'Fira Code', monospace;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
    line-height: 1.5;
  }
  .hbtn:hover:not(:disabled) { border-color: #3a4555; background: #253040; color: #fff; }
  .hbtn:disabled { opacity: 0.35; cursor: not-allowed; }
  .hbtn.primary { border-color: #22c55e55; color: #22c55e; }
  .hbtn.primary:hover:not(:disabled) { background: #22c55e22; border-color: #22c55e; }
  .hbtn.danger { border-color: #ef444455; color: #ef4444; }
  .hbtn.danger:hover:not(:disabled) { background: #ef444422; border-color: #ef4444; }

  .hijack-prompt {
    margin-left: auto;
    font-size: 11px;
    color: #4a6080;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 220px;
  }

  /* ── Terminal area ── */
  .hijack-terminal {
    flex: 1;
    min-height: 0;
    overflow: hidden;
    position: relative;
  }

  /* ── Text input row (visible when hijacked by me) ── */
  .hijack-input-row {
    display: none;
    align-items: center;
    gap: 6px;
    padding: 5px 8px;
    background: #141920;
    border-top: 1px solid #1e2530;
    flex-shrink: 0;
  }
  .hijack-input-row.visible { display: flex; }
  .hijack-input-row::before {
    content: '$';
    font-family: 'Fira Code', monospace;
    font-size: 12px;
    color: #22c55e;
    padding: 0 4px 0 8px;
    flex-shrink: 0;
  }
  .hijack-input-field {
    flex: 1;
    background: #0d1117;
    border: 1px solid #2a3040;
    border-radius: 4px;
    color: #e2e8f0;
    font-family: 'Fira Code', 'Cascadia Code', monospace;
    font-size: 13px;
    padding: 3px 8px;
    outline: none;
  }
  .hijack-input-field:focus { border-left: 2px solid #22c55e; border-color: #22c55e66; }
  .hijack-input-send {
    padding: 3px 10px;
    border-radius: 4px;
    border: 1px solid #22c55e55;
    background: #22c55e22;
    color: #22c55e;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
  }
  .hijack-input-send:hover { background: #22c55e33; border-color: #22c55e; }

  /* ── Analysis panel ── */
  .hijack-analysis {
    border-top: 1px solid #1e2530;
    flex-shrink: 0;
    max-height: 130px;
    overflow-y: auto;
    background: #0d1117;
  }
  .hijack-analysis summary {
    padding: 4px 10px;
    font-size: 11px;
    color: #4a6080;
    cursor: pointer;
    user-select: none;
    background: #141920;
    border-bottom: 1px solid #1e2530;
    list-style: none;
  }
  .hijack-analysis summary::-webkit-details-marker { display: none; }
  .hijack-analysis summary::before { content: '▶ '; font-size: 9px; }
  .hijack-analysis[open] summary::before { content: '▼ '; font-size: 9px; }
  .hijack-analysis summary:hover { color: #8899aa; }
  .hijack-analysis pre {
    margin: 0;
    padding: 8px 12px;
    font-size: 12px;
    color: #99aabb;
    white-space: pre-wrap;
    word-break: break-all;
    font-family: 'Fira Code', 'Cascadia Code', monospace;
  }

  /* ── Scrollbar ── */
  .hijack-analysis::-webkit-scrollbar { width: 5px; }
  .hijack-analysis::-webkit-scrollbar-track { background: transparent; }
  .hijack-analysis::-webkit-scrollbar-thumb { background: #2a3040; border-radius: 3px; }

  .hijack-terminal::-webkit-scrollbar { width: 4px; }
  .hijack-terminal::-webkit-scrollbar-track { background: transparent; }
  .hijack-terminal::-webkit-scrollbar-thumb { background: rgba(100,120,150,0.3); border-radius: 2px; }

  /* ── Mobile key toolbar ── */
  .mobile-keys {
    display: none;
    flex-wrap: wrap;
    gap: 4px;
    padding: 5px 8px;
    background: #141920;
    border-top: 1px solid #1e2530;
    flex-shrink: 0;
  }
  .mobile-keys.visible { display: flex; }
  .mkey {
    padding: 4px 9px;
    border-radius: 4px;
    border: 1px solid #2a3040;
    background: #1e2530;
    color: #ccd6e8;
    font-family: 'Fira Code', 'Cascadia Code', monospace;
    font-size: 11px;
    cursor: pointer;
    white-space: nowrap;
    line-height: 1.4;
    transition: background 0.1s, border-color 0.1s;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }
  .mkey:hover { background: #253040; border-color: #3a4555; color: #fff; }
  .mkey:active { background: #22c55e22; border-color: #22c55e66; }
`;

@customElement("uterm-session")
export class UtermSessionElement extends LitElement {
  @property({ type: Object }) config: Partial<HijackConfig> = {};
  @property({ type: Number }) uid = 0;
  /**
   * Terminal-only embed mode: when set (e.g. `<uterm-session chromeless>`), the
   * toolbar, input row, mobile keys and analysis panel are not rendered, leaving
   * the terminal to fill the host. Reflected so external code (demo recorders,
   * embedders) can toggle it with `setAttribute("chromeless", "")` regardless of
   * how the element was mounted. A client-only render flag — not a wire field.
   */
  @property({ type: Boolean, reflect: true }) chromeless = false;

  @state() private _statusLevel = "bad";
  @state() private _statusText = "Connecting…";
  @state() private _mobileKeysVisible = false;
  @state() private _promptId = "";
  @state() private _analysisText = "(no analysis)";
  @state() private _analysisOpen = false;
  @state() private _pendingApproval: { id: string; command: string; expiresAt: number } | null = null;
  @state() private _connected = false;

  @query("#terminal") terminalElement!: HTMLElement;
  @query("#inputfield") inputField!: HTMLInputElement;

  private _resolvedConfig!: ResolvedConfig;
  private _hijackState!: HijackState;
  private _handlers!: HijackHandlers;

  private _fitAddon: FitAddonInstance | null = null;
  private _ro: ResizeObserver | null = null;
  private _activityFlashTimer: ReturnType<typeof setTimeout> | null = null;

  static override styles = [hijackStyles];

  // Provide public API mirroring original ProvideHijack
  get terminal(): XTerminal | null {
    return this._hijackState?.term ?? null;
  }

  override connectedCallback() {
    super.connectedCallback();
    this._init();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    this.disconnect();
    this.dispose();
  }

  private _init() {
    this._resolvedConfig = {
      wsUrl: this.config.wsUrl,
      workerId: this.config.workerId,
      wsPathPrefix: this.config.wsPathPrefix ?? "/ws/browser",
      authToken: this.config.authToken,
      title: this.config.title,
      showInput: this.config.showInput ?? true,
      showAnalysis: this.config.showAnalysis ?? true,
      heartbeatInterval: this.config.heartbeatInterval ?? 5000,
      mobileKeys: this.config.mobileKeys ?? true,
      role: this.config.role,
      onResize: this.config.onResize,
      onPresenceMessage: this.config.onPresenceMessage,
      approvalUxMode: this.config.approvalUxMode ?? "auto",
    };

    this._hijackState = new HijackState(
      this._resolvedConfig,
      this.config.workerId ?? "default",
      new ControlChannelDecoder(),
    );

    this._handlers = {
      setStatus: (level, text) => {
        this._statusLevel = level;
        this._statusText = text;
      },
      updateStatus: () => this._updateStatus(),
      updateButtons: () => this.requestUpdate(),
      handleMessage: (msg) => this._handleMessage(msg),
    };
  }

  connect(): void {
    // config may be assigned AFTER the element mounts (e.g. React renders the
    // <uterm-session> tag, then sets .config and calls connect() in an effect),
    // so _init() ran with the default workerId. Re-resolve if the workerId now
    // differs — but not on a plain reconnect (config unchanged), which must keep
    // the existing HijackState (resume token, reconnect counters, …).
    if (this._hijackState && this.config.workerId && this._hijackState.workerId !== this.config.workerId) {
      this._init();
    }
    if (this._hijackState && this._handlers) {
      connectWs(this._hijackState, this._handlers);
    }
  }

  disconnect(): void {
    if (!this._hijackState) return;
    clearHeartbeat(this._hijackState);
    if (this._ro) {
      this._ro.disconnect();
      this._ro = null;
    }
    if (this._hijackState.reconnectTimer) {
      clearTimeout(this._hijackState.reconnectTimer);
      this._hijackState.reconnectTimer = null;
    }
    if (this._hijackState.ws) {
      try {
        this._hijackState.ws.close();
      } catch (err) {
        console.debug("ProvideHijack: ws.close() failed during disconnect (best-effort cleanup)", err);
      }
      this._hijackState.ws = null;
    }
  }

  dispose(): void {
    if (!this._hijackState) return;
    this.disconnect();
    stopReconnectAnim(this._hijackState);
    this._pendingApproval = null;
    if (this._activityFlashTimer) {
      clearTimeout(this._activityFlashTimer);
      this._activityFlashTimer = null;
    }
    if (this._hijackState.term) {
      this._hijackState.term.dispose();
      this._hijackState.term = null;
    }
    this._fitAddon = null;
    this.remove();
  }

  sendControlMessage(msg: Record<string, unknown>): void {
    wsSend(this._hijackState, msg);
  }

  private _ensureTerm(): XTerminal {
    if (this._hijackState.term) return this._hijackState.term;
    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const terminalCtor = (window as any).Terminal as (new (opts: Record<string, unknown>) => XTerminal) | undefined;
    if (!terminalCtor) throw new Error("xterm.js not loaded");

    const termDiv = this.terminalElement;
    if (!termDiv) throw new Error("terminal container not found");
    const term = new terminalCtor({
      convertEol: false,
      cursorBlink: true,
      fontFamily: "'Fira Code', 'Cascadia Code', 'Consolas', monospace",
      fontSize: 13,
      theme: { background: "#0b0f14" },
      allowTransparency: true,
      scrollback: 10000,
      scrollOnUserInput: true,
    });
    this._hijackState.term = term;
    term.open(termDiv);
    term.focus();

    if (this._resolvedConfig.onPresenceMessage) {
      term.onScroll((viewportY: number) => {
        const rows = this._hijackState.term?.rows ?? 25;
        const totalLines = (this._hijackState.term?.buffer?.active?.length as number | undefined) ?? rows;
        this.dispatchEvent(
          new CustomEvent("uterm:scroll", { bubbles: false, detail: { viewportY, rows, totalLines } }),
        );
      });
    }

    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const fitAddonGlobal = ((window as any).FitAddon ?? (globalThis as any).FitAddon) as
      | { FitAddon: new () => FitAddonInstance }
      | undefined;
    if (fitAddonGlobal) {
      this._fitAddon = new fitAddonGlobal.FitAddon();
      term.loadAddon(this._fitAddon);
      requestAnimationFrame(() => {
        try {
          this._fitAddon?.fit();
          if (this._hijackState.term && this._hijackState.term.cols > 0 && this._hijackState.term.rows > 0) {
            this._resolvedConfig.onResize?.(this._hijackState.term.cols, this._hijackState.term.rows);
          }
        } catch (err) {
          console.debug("ProvideHijack: fit() failed in requestAnimationFrame (best-effort resize)", err);
        }
      });
      this._ro = new ResizeObserver(() => {
        try {
          this._fitAddon?.fit();
          if (this._hijackState.term && this._hijackState.term.cols > 0 && this._hijackState.term.rows > 0) {
            this._resolvedConfig.onResize?.(this._hijackState.term.cols, this._hijackState.term.rows);
          }
        } catch (err) {
          console.debug("ProvideHijack: fit() failed in ResizeObserver callback (best-effort resize)", err);
        }
      });
      this._ro.observe(termDiv);
    }

    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const webLinksAddonGlobal = (window as any).WebLinksAddon ?? (globalThis as any).WebLinksAddon;
    if (webLinksAddonGlobal) {
      try {
        term.loadAddon(new webLinksAddonGlobal.WebLinksAddon());
      } catch (err) {
        console.warn("ProvideHijack: WebLinksAddon failed to load (optional addon, skipping)", err);
      }
    }

    // Forward keyboard input to WS when hijacked or in open mode.
    term.onData((data: string) => {
      if (!this._hijackState.ws || this._hijackState.ws.readyState !== WebSocket.OPEN) {
        nudgeReconnect(this._hijackState, this._handlers);
        return;
      }
      if (this._hijackState.inputMode !== "open" && !this._hijackState.hijackedByMe) return;
      // Show activity indicator (no local echo — let server echo drive the display).
      this._showActivityIndicator();
      wsSend(this._hijackState, { type: "input", data });
    });

    return term;
  }

  private _showActivityIndicator(): void {
    const dot = this.shadowRoot?.getElementById("dot");
    if (!dot) return;

    if (this._activityFlashTimer) clearTimeout(this._activityFlashTimer);
    dot.classList.add("activity-flash");

    this._activityFlashTimer = setTimeout(() => {
      dot.classList.remove("activity-flash");
      this._activityFlashTimer = null;
    }, 200);
  }

  private _handleMessage(rawMsg: Record<string, unknown>): void {
    const msg = rawMsg as AnyFrame;
    const state = this._hijackState;
    switch (msg.type) {
      case "term":
        state.workerOnline = true;
        if (msg.data) {
          try {
            this._ensureTerm().write(msg.data);
          } catch (err) {
            console.warn("ProvideHijack: term write failed for 'term' frame", err);
          }
        }
        break;

      case "snapshot": {
        stopReconnectAnim(state);
        state.workerOnline = true;
        const promptDetected = msg.prompt_detected as { prompt_id?: string } | null | undefined;
        this._promptId = promptDetected?.prompt_id ?? "";
        try {
          const t = this._ensureTerm();
          t.write("\x1b[!p\x1b[2J\x1b[H");
          t.write((msg.screen ?? "").replace(/\n/g, "\r\n"));
        } catch (err) {
          console.warn("ProvideHijack: snapshot write failed", err);
        }
        break;
      }

      case "analysis": {
        this._analysisText = msg.formatted ?? "(no analysis)";
        this._analysisOpen = true;
        break;
      }

      case "hello": {
        state.canHijack = !!msg.can_hijack;
        state.hijacked = !!msg.hijacked;
        state.hijackedByMe = !!msg.hijacked_by_me;
        state.workerOnline = !!msg.worker_online;
        if (msg.input_mode) state.inputMode = msg.input_mode;
        if (msg.role) state.serverRole = msg.role;
        const caps = msg.capabilities;
        const capsHijackControl = caps?.hijack_control as string | undefined;
        const capsHijackStep = caps?.hijack_step_supported as boolean | undefined;
        state.hijackControl = msg.hijack_control ?? capsHijackControl ?? "ws";
        const stepSupported = msg.hijack_step_supported ?? capsHijackStep;
        state.hijackStepSupported = stepSupported !== false;
        if (msg.resume_token) {
          state.resumeToken = msg.resume_token;
          saveResumeToken(state, msg.resume_token);
        }
        this._updateStatus();
        this.requestUpdate();
        break;
      }

      case "worker_connected":
        if (state.wakingTimer) {
          clearTimeout(state.wakingTimer);
          state.wakingTimer = null;
        }
        state.wakingTimedOut = false;
        state.workerOnline = true;
        this._updateStatus();
        this.requestUpdate();
        break;

      case "hijack_state": {
        state.hijacked = msg.hijacked;
        state.hijackedByMe = msg.owner === "me";
        if (!state.hijackedByMe) state.restHijackId = null;
        if (msg.input_mode) state.inputMode = msg.input_mode;
        if (state.hijackedByMe) {
          startHeartbeat(state);
        } else {
          clearHeartbeat(state);
        }
        this._updateStatus();
        this.requestUpdate();
        break;
      }

      case "worker_disconnected":
        if (state.wakingTimer) {
          clearTimeout(state.wakingTimer);
          state.wakingTimer = null;
        }
        state.wakingTimedOut = true;
        state.workerOnline = false;
        state.hijacked = false;
        state.hijackedByMe = false;
        clearHeartbeat(state);
        this._updateStatus();
        this.requestUpdate();
        break;

      case "input_mode_changed": {
        if (msg.input_mode) state.inputMode = msg.input_mode;
        this._updateStatus();
        this.requestUpdate();
        break;
      }

      case "heartbeat_ack":
        break;

      case "approval_pending":
        this._pendingApproval = {
          id: msg.request_id,
          command: msg.command,
          expiresAt: msg.expires_at,
        };
        break;

      case "approval_resolved":
        this._pendingApproval = null;
        break;

      case "error": {
        const message = msg.message ?? "unknown";
        if (message === "Buffer overflow — input dropped") {
          this._handlers.setStatus("bad", message);
        } else {
          this._handlers.setStatus("bad", `Error: ${message}`);
        }
        if (message.toLowerCase().includes("ownership mismatch")) {
          wsSend(state, { type: "snapshot_req" });
        }
        break;
      }

      case "presence_sync":
      case "presence_update":
      case "presence_leave":
      case "control_transfer":
        this._resolvedConfig.onPresenceMessage?.(rawMsg);
        break;
    }
  }

  private _updateStatus(): void {
    const state = this._hijackState;
    const connected = !!(state.ws && state.ws.readyState === WebSocket.OPEN);
    this._connected = connected;
    if (!connected) {
      this._handlers.setStatus("bad", "Disconnected");
    } else if (!state.workerOnline) {
      if (state.wakingTimedOut) {
        this._handlers.setStatus("bad", "Offline");
      } else {
        this._handlers.setStatus("warn", "Waking…");
      }
    } else if (state.hijackedByMe) {
      this._handlers.setStatus("live", "Hijacked (you)");
    } else if (state.hijacked) {
      this._handlers.setStatus("warn", "Hijacked (other)");
    } else if (state.inputMode === "open") {
      this._handlers.setStatus("live", "Connected (shared)");
    } else {
      this._handlers.setStatus("live", "Connected (watching)");
    }
  }

  private _effectiveRole(): string | undefined {
    return this._hijackState.serverRole ?? this._resolvedConfig.role;
  }

  private _getEffectiveUxMode(): "modal" | "statusbar" {
    const mode = this._resolvedConfig.approvalUxMode;
    if (mode === "auto") {
      return this._effectiveRole() === "admin" ? "modal" : "statusbar";
    }
    return mode;
  }

  private async _handleApprovalAction(e: CustomEvent) {
    if (!this._pendingApproval) return;
    const action = e.detail as "approve" | "reject";
    const el = this.shadowRoot?.querySelector("uterm-approval-prompt") as ApprovalPromptElement | null;
    const btn = el?.shadowRoot?.querySelector(`.hijack-btn-${action}`) as HTMLButtonElement | null;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Sending...";
    }

    try {
      const url = `${location.origin}/api/approvals/${this._pendingApproval.id}/${action}`;
      const resp = await fetch(url, { method: "POST", credentials: "include" });
      if (!resp.ok) {
        throw new Error(`Failed to ${action}: ${resp.statusText}`);
      }
    } catch (err) {
      console.error(err);
      if (btn) {
        btn.disabled = false;
        btn.textContent = action.charAt(0).toUpperCase() + action.slice(1);
      }
      this._handlers.setStatus("bad", `Failed to ${action} command`);
    }
  }

  private _handleApprovalExpired() {
    this._pendingApproval = null;
  }

  private _doHijack() {
    const state = this._hijackState;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    if (state.hijackControl === "rest") {
      restHijack(state, "acquire", { owner: "dashboard", lease_s: 60 })
        .then((data) => {
          if (data && typeof data.hijack_id === "string") state.restHijackId = data.hijack_id;
        })
        .finally(() => wsSend(state, { type: "snapshot_req" }));
      return;
    }
    wsSend(state, { type: "hijack_request" });
  }

  private _doStep() {
    const state = this._hijackState;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    if (!state.hijackedByMe) return;
    if (state.hijackControl === "rest") {
      restHijack(state, "step").catch(() => {});
    } else {
      wsSend(state, { type: "hijack_step" });
    }
    for (const ms of [250, 1000]) {
      setTimeout(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) wsSend(state, { type: "snapshot_req" });
      }, ms);
    }
    for (const ms of [450, 1200]) {
      setTimeout(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) wsSend(state, { type: "analyze_req" });
      }, ms);
    }
  }

  private _doRelease() {
    const state = this._hijackState;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    if (state.hijackControl === "rest") {
      restHijack(state, "release")
        .then(() => {
          state.restHijackId = null;
        })
        .finally(() => wsSend(state, { type: "snapshot_req" }));
      return;
    }
    wsSend(state, { type: "hijack_release" });
  }

  private _doResync() {
    const state = this._hijackState;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    wsSend(state, { type: "snapshot_req" });
  }

  private _doAnalyze() {
    const state = this._hijackState;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    if (!state.hijackedByMe) return;
    wsSend(state, { type: "analyze_req" });
  }

  private _sendInput() {
    const raw = this.inputField?.value;
    const state = this._hijackState;
    if (!raw || (state.inputMode !== "open" && !state.hijackedByMe)) return;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    const data = raw.replace(/\\r/g, "\r").replace(/\\n/g, "\n").replace(/\\t/g, "\t").replace(/\\e/g, "\x1b");
    this._showActivityIndicator();
    wsSend(state, { type: "input", data });
    this.inputField.value = "";
    try {
      this._ensureTerm().focus();
    } catch (err) {
      console.debug("ProvideHijack: focus() failed after input send (best-effort UX)", err);
    }
  }

  private _onInputKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      this._sendInput();
    }
  }

  private _sendMkey(data: string) {
    const state = this._hijackState;
    if (state.inputMode !== "open" && !state.hijackedByMe) return;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    this._showActivityIndicator();
    wsSend(state, { type: "input", data });
  }

  override render() {
    const state = this._hijackState;
    if (!state) return html``;
    const title =
      this._resolvedConfig?.title ?? (this._resolvedConfig?.workerId ? this._resolvedConfig.workerId : "Terminal");

    const connected = this._connected;
    const isOpen = state.inputMode === "open";
    const hideHijack = isOpen || !state.canHijack;
    const canInput = state.hijackedByMe || isOpen;
    // Terminal-only embed: skip every chrome surface and let the terminal fill
    // the host (`.hijack-terminal` is already flex:1 inside the column :host).
    const chrome = !this.chromeless;

    return html`
      ${
        chrome
          ? html`
      <div class="hijack-toolbar">
        <span class="hijack-title">${title}</span>
        <span class="hijack-status">
          <span class="hijack-status-dot ${this._statusLevel}" id="dot"></span>
          <span id="statustext">${this._statusText}</span>
        </span>
        <div class="hijack-controls">
          <button class="hbtn primary" id="hijack"
            .disabled=${!connected || hideHijack || state.hijacked || !state.workerOnline}
            style=${hideHijack ? "display: none" : ""}
            @click=${this._doHijack} title="Take exclusive control">Hijack</button>
          <button class="hbtn" id="step"
            .disabled=${!connected || hideHijack || !state.hijackedByMe || !state.hijackStepSupported}
            style=${hideHijack ? "display: none" : ""}
            @click=${this._doStep} title="Send one step, then pause">Step</button>
          <button class="hbtn danger" id="release"
            .disabled=${!connected || hideHijack || !state.hijackedByMe}
            style=${hideHijack ? "display: none" : ""}
            @click=${this._doRelease} title="Release hijack control">Release</button>
          <button class="hbtn" id="resync"
            .disabled=${!connected || !state.workerOnline}
            @click=${this._doResync} title="Request full screen snapshot">⟳ Resync</button>
          <button class="hbtn" id="analyze"
            .disabled=${!connected || hideHijack || !state.hijackedByMe}
            @click=${this._doAnalyze} title="AI-readable screen description">Analyze</button>
          <button class="hbtn" id="kbdtoggle"
            @click=${() => (this._mobileKeysVisible = !this._mobileKeysVisible)} title="Toggle mobile key toolbar">⌨</button>
        </div>
        <span class="hijack-prompt" id="prompt" title="Current prompt ID">${this._promptId ? `prompt: ${this._promptId}` : ""}</span>
      </div>`
          : ""
      }
      <div class="hijack-terminal" id="terminal"></div>
      ${
        chrome
          ? html`
      <div class="hijack-input-row ${connected && canInput && this._resolvedConfig.showInput ? "visible" : ""}" id="inputrow">
        <input class="hijack-input-field" id="inputfield"
          placeholder="Send keys… (Enter to send, e.g. \\r for Return)"
          autocomplete="off" spellcheck="false"
          @keydown=${this._onInputKeydown}>
        <button class="hijack-input-send" id="inputsend" @click=${this._sendInput}>Send</button>
      </div>`
          : ""
      }
      ${
        chrome && this._resolvedConfig.mobileKeys
          ? html`
        <div class="mobile-keys ${connected && canInput && this._mobileKeysVisible ? "visible" : ""}" id="mobilekeys">
          ${MOBILE_KEYS.map((k) => html`<button class="mkey" @click=${() => this._sendMkey(k.data)}>${k.label}</button>`)}
        </div>
      `
          : ""
      }
      ${
        chrome && this._resolvedConfig.showAnalysis
          ? html`
        <details class="hijack-analysis" id="analysis" ?open=${this._analysisOpen} @toggle=${(e: Event) => (this._analysisOpen = (e.target as HTMLDetailsElement).open)}>
          <summary>Analysis</summary>
          <pre id="analysistext">${this._analysisText}</pre>
        </details>
      `
          : ""
      }
      ${
        this._pendingApproval
          ? html`
        <uterm-approval-prompt
          .uid=${this.uid}
          .mode=${this._getEffectiveUxMode()}
          .isAdmin=${this._effectiveRole() === "admin"}
          .pendingApproval=${this._pendingApproval}
          @approval-action=${this._handleApprovalAction}
          @approval-expired=${this._handleApprovalExpired}
        ></uterm-approval-prompt>
      `
          : ""
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-session": UtermSessionElement;
  }
}
