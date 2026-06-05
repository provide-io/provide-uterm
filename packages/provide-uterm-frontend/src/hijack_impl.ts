//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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

import type { AnyFrame } from "./generated/frames.js";
import {
  ControlChannelDecoder,
  type FitAddonInstance,
  type HijackConfig,
  type ResolvedConfig,
  type XTerminal,
} from "./hijack-codec.js";
import {
  approvalElementClass,
  buildApprovalModalHtml,
  buildApprovalStatusBarHtml,
  computeRemainingSeconds,
} from "./hijack-approval.js";
import {
  buildHijackToolbarHtml,
  escapeHijackHtml,
  injectHijackCss,
  MOBILE_KEYS,
} from "./hijack-ui.js";
import { HijackState, type HijackHandlers } from "./hijack-state.js";
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

// ── Module-level guards ───────────────────────────────────────────────────────
let _hijackInstanceCount = 0;
// Resolve CSS base URL via import.meta.url (works for ES modules; document.currentScript is null for modules)
const _hijackCssBase = new URL("./", import.meta.url).href;

function _injectHijackCSS(): void {
  injectHijackCss(_hijackCssBase);
}

// ── ProvideHijack class ─────────────────────────────────────────────────────────
export class ProvideHijack {
  private readonly _container: HTMLElement;
  private readonly _config: ResolvedConfig;
  private readonly _uid: number;
  private readonly _state: HijackState;
  private readonly _handlers: HijackHandlers;

  private _fitAddon: FitAddonInstance | null = null;
  private _ro: ResizeObserver | null = null;

  private _mobileKeysVisible = false;
  private _activityFlashTimer: ReturnType<typeof setTimeout> | null = null;
  private _statusDotElement: HTMLElement | null = null;
  private _root: HTMLElement | null = null;
  private _pendingApproval: { id: string; command: string; expiresAt: number } | null = null;
  private _approvalElement: HTMLElement | null = null;
  private _approvalTimer: ReturnType<typeof setInterval> | null = null;

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
      onPresenceMessage: config.onPresenceMessage,
      approvalUxMode: config.approvalUxMode ?? "auto",
    };
    this._uid = ++_hijackInstanceCount;
    this._state = new HijackState(this._config, config.workerId ?? "default", new ControlChannelDecoder());
    this._handlers = {
      setStatus: (level, text) => this._setStatus(level, text),
      updateStatus: () => this._updateStatus(),
      updateButtons: () => this._updateButtons(),
      handleMessage: (msg) => this._handleMessage(msg),
    };

    _injectHijackCSS();
    this._buildDOM();
    this.connect();
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  /** Open the WebSocket connection. Called automatically on construction. */
  connect(): void {
    connectWs(this._state, this._handlers);
  }

  /** Close the WebSocket connection. */
  disconnect(): void {
    clearHeartbeat(this._state);
    if (this._ro) {
      this._ro.disconnect();
      this._ro = null;
    }
    if (this._state.reconnectTimer) {
      clearTimeout(this._state.reconnectTimer);
      this._state.reconnectTimer = null;
    }
    if (this._state.ws) {
      try {
        this._state.ws.close();
      } catch (_) {}
      this._state.ws = null;
    }
  }

  /** Tear down entirely: xterm, WebSocket, ResizeObserver, and DOM. */
  dispose(): void {
    this.disconnect();
    stopReconnectAnim(this._state);
    // Clear the approval countdown interval + widget; otherwise disposing while
    // an approval_pending UI is up leaks a setInterval that keeps firing against
    // the torn-down widget until the approval would have expired.
    this._hideApprovalUI();
    this._pendingApproval = null;
    if (this._activityFlashTimer) {
      clearTimeout(this._activityFlashTimer);
      this._activityFlashTimer = null;
    }
    if (this._state.term) {
      this._state.term.dispose();
      this._state.term = null;
    }
    this._fitAddon = null;
    this._statusDotElement = null;
    if (this._root?.parentNode) {
      this._root.parentNode.removeChild(this._root);
    }
    this._root = null;
  }

  /** Send a control message over the WebSocket (e.g. presence_update, control_request). */
  sendControlMessage(msg: Record<string, unknown>): void {
    wsSend(this._state, msg);
  }

  /** The terminal wrapper element, available immediately after construction. */
  get terminalElement(): HTMLElement | null {
    return this._q("terminal");
  }

  /** The xterm.js Terminal instance, or null if not yet initialized. */
  get terminal(): XTerminal | null {
    return this._state.term;
  }

  // Back-compat shim: tests and external callers access `_term` via the `any` cast
  // to inspect the underlying xterm instance. Keep it as a getter onto state.
  private get _term(): XTerminal | null {
    return this._state.term;
  }

  // ── Internal helpers ────────────────────────────────────────────────────────

  /** Query by ID within this instance's root (IDs are prefixed with h-{uid}-). */
  private _q(id: string): HTMLElement | null {
    return this._root?.querySelector<HTMLElement>(`#h-${this._uid}-${id}`) ?? null;
  }

  // ── DOM Construction ────────────────────────────────────────────────────────

  private _buildDOM(): void {
    const workerId = this._config.workerId ?? "";
    const title = this._config.title ?? (workerId ? workerId : "Terminal");

    const root = document.createElement("div");
    root.className = "provide-hijack";
    root.innerHTML = buildHijackToolbarHtml({
      uid: this._uid,
      title,
      showAnalysis: this._config.showAnalysis,
    });

    this._root = root;
    this._container.appendChild(root);

    // Expose widget on the root element for programmatic access (demo recording, tests).
    // biome-ignore lint/suspicious/noExplicitAny: bridge for external callers
    (root as any).__provideHijack = this;

    this._bindEvents();
  }

  // ── xterm ─────────────────────────────────────────────────────────────────

  private _ensureTerm(): XTerminal {
    if (this._state.term) return this._state.term;
    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const terminalCtor = (window as any).Terminal as (new (opts: Record<string, unknown>) => XTerminal) | undefined;
    if (!terminalCtor) throw new Error("xterm.js not loaded");

    const termDiv = this._q("terminal");
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
    this._state.term = term;
    term.open(termDiv);
    term.focus();

    if (this._config.onPresenceMessage) {
      term.onScroll((viewportY: number) => {
        const rows = this._state.term?.rows ?? 25;
        const totalLines = (this._state.term?.buffer?.active?.length as number | undefined) ?? rows;
        this._container.dispatchEvent(
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
          if (this._state.term && this._state.term.cols > 0 && this._state.term.rows > 0) {
            this._config.onResize?.(this._state.term.cols, this._state.term.rows);
          }
        } catch (_) {}
      });
      this._ro = new ResizeObserver(() => {
        try {
          this._fitAddon?.fit();
          if (this._state.term && this._state.term.cols > 0 && this._state.term.rows > 0) {
            this._config.onResize?.(this._state.term.cols, this._state.term.rows);
          }
        } catch (_) {}
      });
      this._ro.observe(termDiv);
    }

    // biome-ignore lint/suspicious/noExplicitAny: window global access
    const webLinksAddonGlobal = (window as any).WebLinksAddon ?? (globalThis as any).WebLinksAddon;
    if (webLinksAddonGlobal) {
      try {
        term.loadAddon(new webLinksAddonGlobal.WebLinksAddon());
      } catch (_) {}
    }

    // Forward keyboard input to WS when hijacked or in open mode.
    term.onData((data: string) => {
      if (!this._state.ws || this._state.ws.readyState !== WebSocket.OPEN) {
        nudgeReconnect(this._state, this._handlers);
        return;
      }
      if (this._state.inputMode !== "open" && !this._state.hijackedByMe) return;
      // Show activity indicator (no local echo — let server echo drive the display).
      this._showActivityIndicator();
      wsSend(this._state, { type: "input", data });
    });

    return term;
  }

  /** Show activity indicator with the configured style (reuses DOM reference and timeout). */
  private _showActivityIndicator(): void {
    if (!this._statusDotElement) {
      this._statusDotElement = this._q("dot");
    }
    const dot = this._statusDotElement;
    if (!dot) return;

    if (this._activityFlashTimer) clearTimeout(this._activityFlashTimer);
    dot.classList.add("activity-flash");

    this._activityFlashTimer = setTimeout(() => {
      dot.classList.remove("activity-flash");
      this._activityFlashTimer = null;
    }, 200);
  }

  private _buildMobileKeys(): void {
    const container = this._q("mobilekeys");
    if (!container) return;
    for (const { label, data } of MOBILE_KEYS) {
      const btn = document.createElement("button");
      btn.className = "mkey";
      btn.textContent = label;
      btn.addEventListener("click", () => {
        if (this._state.inputMode !== "open" && !this._state.hijackedByMe) return;
        if (!this._state.ws || this._state.ws.readyState !== WebSocket.OPEN) return;
        this._showActivityIndicator();
        wsSend(this._state, { type: "input", data });
      });
      container.appendChild(btn);
    }
  }

  // ── Message dispatch ──────────────────────────────────────────────────────

  private _handleMessage(rawMsg: Record<string, unknown>): void {
    // Trust the decoder's parsed control payload as conforming to the
    // generated AnyFrame union — wire format is the single source of truth
    // (packages/provide-uterm/.../bridge/schemas.py).
    const msg = rawMsg as AnyFrame;
    const state = this._state;
    switch (msg.type) {
      case "term":
        state.workerOnline = true;
        if (msg.data) {
          try {
            this._ensureTerm().write(msg.data);
          } catch (_) {}
        }
        break;

      case "snapshot": {
        stopReconnectAnim(state);
        state.workerOnline = true;
        const promptDetected = msg.prompt_detected as { prompt_id?: string } | null | undefined;
        const promptId = promptDetected?.prompt_id;
        this._setPromptId(promptId ?? "");
        try {
          const t = this._ensureTerm();
          // Use ANSI soft reset (DECSTR) + clear screen — preserves scrollback buffer.
          // Avoids t.reset() which destroys scrollback and breaks scroll indicators.
          t.write("[!p[2J[H");
          t.write((msg.screen ?? "").replace(/\n/g, "\r\n"));
        } catch (_) {}
        break;
      }

      case "analysis": {
        const pre = this._q("analysistext");
        if (pre) {
          pre.textContent = msg.formatted ?? "(no analysis)";
          const details = this._q("analysis");
          if (details) (details as HTMLDetailsElement).open = true;
        }
        break;
      }

      case "hello": {
        state.canHijack = !!msg.can_hijack;
        state.hijacked = !!msg.hijacked;
        state.hijackedByMe = !!msg.hijacked_by_me;
        state.workerOnline = !!msg.worker_online;
        if (msg.input_mode) state.inputMode = msg.input_mode;
        // Server is authoritative on role; prefer it over the constructor
        // input for UX decisions (approval modal vs statusbar, admin buttons).
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
        this._updateButtons();
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
        this._updateButtons();
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
        this._updateButtons();
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
        this._updateButtons();
        break;

      case "input_mode_changed": {
        if (msg.input_mode) state.inputMode = msg.input_mode;
        this._updateStatus();
        this._updateButtons();
        break;
      }

      case "heartbeat_ack":
        break; // lease refreshed — no visible change needed

      case "approval_pending":
        this._pendingApproval = {
          id: msg.request_id,
          command: msg.command,
          expiresAt: msg.expires_at,
        };
        this._showApprovalUI();
        break;

      case "approval_resolved":
        this._pendingApproval = null;
        this._hideApprovalUI();
        break;

      case "error": {
        const message = msg.message ?? "unknown";
        if (message === "Buffer overflow — input dropped") {
          this._setStatus("bad", message);
        } else {
          this._setStatus("bad", `Error: ${message}`);
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
        this._config.onPresenceMessage?.(rawMsg);
        break;

      default:
        // Frame types that exist in AnyFrame but aren't handled here
        // (input, snapshot_req, hijack_request/release/step, worker_hello,
        // heartbeat, ping, pong, resume, status). An exhaustive default
        // makes adding a new frame type a compile-time decision.
        break;
    }
  }

  // ── UI State ──────────────────────────────────────────────────────────────

  /**
   * Effective role: prefer the server-confirmed `serverRole` (set from the
   * `hello` frame) over the constructor-input `config.role`. UX decisions
   * should follow what the server says, not what the caller claimed.
   */
  private _effectiveRole(): string | undefined {
    return this._state.serverRole ?? this._config.role;
  }

  private _getEffectiveUxMode(): "modal" | "statusbar" {
    const mode = this._config.approvalUxMode;
    if (mode === "auto") {
      return this._effectiveRole() === "admin" ? "modal" : "statusbar";
    }
    return mode;
  }

  private _showApprovalUI(): void {
    if (!this._pendingApproval || !this._root) return;
    this._hideApprovalUI();

    const mode = this._getEffectiveUxMode();
    const el = document.createElement("div");
    this._approvalElement = el;
    el.className = approvalElementClass(mode);
    const isAdmin = this._effectiveRole() === "admin";
    el.innerHTML =
      mode === "modal"
        ? buildApprovalModalHtml({ uid: this._uid, command: this._pendingApproval.command, isAdmin })
        : buildApprovalStatusBarHtml({ uid: this._uid });

    this._root.appendChild(el);
    this._startApprovalTimer();

    if (isAdmin) {
      this._q("approve")?.addEventListener("click", () => this._resolveApproval("approve"));
      this._q("reject")?.addEventListener("click", () => this._resolveApproval("reject"));
    }
  }

  private _hideApprovalUI(): void {
    if (this._approvalTimer) {
      clearInterval(this._approvalTimer);
      this._approvalTimer = null;
    }
    if (this._approvalElement) {
      this._approvalElement.parentNode?.removeChild(this._approvalElement);
      this._approvalElement = null;
    }
  }

  private _startApprovalTimer(): void {
    const update = () => {
      if (!this._pendingApproval) return;
      const remaining = computeRemainingSeconds(this._pendingApproval.expiresAt);
      const el = this._q("approval-timer");
      if (el) el.textContent = String(remaining);
      if (remaining <= 0) this._hideApprovalUI();
    };
    update();
    this._approvalTimer = setInterval(update, 1000);
  }

  private async _resolveApproval(action: "approve" | "reject"): Promise<void> {
    if (!this._pendingApproval) return;
    const btn = this._q(action) as HTMLButtonElement | null;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Sending...";
    }

    try {
      const url = `${location.origin}/api/approvals/${this._pendingApproval.id}/${action}`;
      // credentials:"include" mirrors restHijack so approve/reject carries the
      // session cookie in cross-origin embeds (tunnel/share) too, not just the
      // default same-origin case.
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
      this._setStatus("bad", `Failed to ${action} command`);
    }
  }

  private _setStatus(level: string, text: string): void {
    const dot = this._q("dot");
    const txt = this._q("statustext");
    if (dot) {
      dot.className = `hijack-status-dot ${level}`;
    }
    if (txt) txt.textContent = text;
  }

  private _updateStatus(): void {
    const state = this._state;
    const connected = !!(state.ws && state.ws.readyState === WebSocket.OPEN);
    if (!connected) {
      this._setStatus("bad", "Disconnected"); // red — WS down
    } else if (!state.workerOnline) {
      if (state.wakingTimedOut) {
        this._setStatus("bad", "Offline"); // red — worker never came online
      } else {
        this._setStatus("warn", "Waking…"); // orange — connected but worker not yet online
      }
    } else if (state.hijackedByMe) {
      this._setStatus("live", "Hijacked (you)");
    } else if (state.hijacked) {
      this._setStatus("warn", "Hijacked (other)");
    } else if (state.inputMode === "open") {
      this._setStatus("live", "Connected (shared)");
    } else {
      this._setStatus("live", "Connected (watching)");
    }

    const canInput = state.hijackedByMe || state.inputMode === "open";
    if (this._config.showInput) {
      const row = this._q("inputrow");
      if (row) row.classList.toggle("visible", connected && canInput);
    }

    if (this._config.mobileKeys) {
      const mkRow = this._q("mobilekeys");
      if (mkRow) mkRow.classList.toggle("visible", connected && canInput && this._mobileKeysVisible);
    }
  }

  private _updateButtons(): void {
    const state = this._state;
    const connected = !!(state.ws && state.ws.readyState === WebSocket.OPEN);
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
    const isOpen = state.inputMode === "open";
    const hideHijack = isOpen || !state.canHijack;
    if (hijackBtn) hijackBtn.disabled = hideHijack || state.hijacked || !state.workerOnline;
    if (stepBtn) stepBtn.disabled = hideHijack || !state.hijackedByMe || !state.hijackStepSupported;
    if (releaseBtn) releaseBtn.disabled = hideHijack || !state.hijackedByMe;
    if (resyncBtn) resyncBtn.disabled = !state.workerOnline;
    if (analyzeBtn) analyzeBtn.disabled = hideHijack || !state.hijackedByMe;
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
    const state = this._state;
    this._q("hijack")?.addEventListener("click", () => {
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
    });

    this._q("step")?.addEventListener("click", () => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      if (!state.hijackedByMe) return;
      if (state.hijackControl === "rest") {
        restHijack(state, "step").catch(() => {});
      } else {
        wsSend(state, { type: "hijack_step" });
      }
      // Request snapshot + analysis shortly after the worker acts.
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
    });

    this._q("release")?.addEventListener("click", () => {
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
    });

    this._q("resync")?.addEventListener("click", () => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      wsSend(state, { type: "snapshot_req" });
    });

    this._q("analyze")?.addEventListener("click", () => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      if (!state.hijackedByMe) return;
      wsSend(state, { type: "analyze_req" });
    });

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

    const inputField = this._q("inputfield") as HTMLInputElement | null;
    const inputSend = this._q("inputsend");
    if (inputField) {
      const doSend = () => {
        const raw = inputField.value;
        if (!raw || (state.inputMode !== "open" && !state.hijackedByMe)) return;
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
        // Unescape \\r → \r, \\n → \n, \\t → \t, \\e → ESC
        const data = raw.replace(/\\r/g, "\r").replace(/\\n/g, "\n").replace(/\\t/g, "\t").replace(/\\e/g, "\x1b");
        this._showActivityIndicator();
        wsSend(state, { type: "input", data });
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

// Re-exported so external code that imported the helper from hijack.ts directly
// still works after the WS-lifecycle extraction.
export { escapeHijackHtml };
