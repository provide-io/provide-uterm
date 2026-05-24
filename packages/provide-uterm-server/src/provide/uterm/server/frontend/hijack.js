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
import { ControlChannelDecoder, } from "./hijack-codec.js";
import { approvalElementClass, buildApprovalModalHtml, buildApprovalStatusBarHtml, computeRemainingSeconds, } from "./hijack-approval.js";
import { buildHijackToolbarHtml, escapeHijackHtml, injectHijackCss, MOBILE_KEYS, } from "./hijack-ui.js";
import { HijackState } from "./hijack-state.js";
import { clearHeartbeat, connectWs, nudgeReconnect, restHijack, saveResumeToken, startHeartbeat, stopReconnectAnim, wsSend, } from "./hijack-websocket.js";
// ── Module-level guards ───────────────────────────────────────────────────────
let _hijackInstanceCount = 0;
// Resolve CSS base URL via import.meta.url (works for ES modules; document.currentScript is null for modules)
const _hijackCssBase = new URL("./", import.meta.url).href;
function _injectHijackCSS() {
    injectHijackCss(_hijackCssBase);
}
// ── ProvideHijack class ─────────────────────────────────────────────────────────
export class ProvideHijack {
    /**
     * Create an embeddable hijack control widget.
     *
     * @param container - Element to mount the widget into.
     * @param config - Configuration options.
     */
    constructor(container, config = {}) {
        this._fitAddon = null;
        this._ro = null;
        this._mobileKeysVisible = false;
        this._activityFlashTimer = null;
        this._statusDotElement = null;
        this._root = null;
        this._pendingApproval = null;
        this._approvalElement = null;
        this._approvalTimer = null;
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
    connect() {
        connectWs(this._state, this._handlers);
    }
    /** Close the WebSocket connection. */
    disconnect() {
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
            }
            catch (_) { }
            this._state.ws = null;
        }
    }
    /** Tear down entirely: xterm, WebSocket, ResizeObserver, and DOM. */
    dispose() {
        this.disconnect();
        stopReconnectAnim(this._state);
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
    sendControlMessage(msg) {
        wsSend(this._state, msg);
    }
    /** The terminal wrapper element, available immediately after construction. */
    get terminalElement() {
        return this._q("terminal");
    }
    /** The xterm.js Terminal instance, or null if not yet initialized. */
    get terminal() {
        return this._state.term;
    }
    // Back-compat shim: tests and external callers access `_term` via the `any` cast
    // to inspect the underlying xterm instance. Keep it as a getter onto state.
    get _term() {
        return this._state.term;
    }
    // ── Internal helpers ────────────────────────────────────────────────────────
    /** Query by ID within this instance's root (IDs are prefixed with h-{uid}-). */
    _q(id) {
        return this._root?.querySelector(`#h-${this._uid}-${id}`) ?? null;
    }
    // ── DOM Construction ────────────────────────────────────────────────────────
    _buildDOM() {
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
        root.__provideHijack = this;
        this._bindEvents();
    }
    // ── xterm ─────────────────────────────────────────────────────────────────
    _ensureTerm() {
        if (this._state.term)
            return this._state.term;
        // biome-ignore lint/suspicious/noExplicitAny: window global access
        const terminalCtor = window.Terminal;
        if (!terminalCtor)
            throw new Error("xterm.js not loaded");
        const termDiv = this._q("terminal");
        if (!termDiv)
            throw new Error("terminal container not found");
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
            term.onScroll((viewportY) => {
                const rows = this._state.term?.rows ?? 25;
                const totalLines = this._state.term?.buffer?.active?.length ?? rows;
                this._container.dispatchEvent(new CustomEvent("uterm:scroll", { bubbles: false, detail: { viewportY, rows, totalLines } }));
            });
        }
        // biome-ignore lint/suspicious/noExplicitAny: window global access
        const fitAddonGlobal = (window.FitAddon ?? globalThis.FitAddon);
        if (fitAddonGlobal) {
            this._fitAddon = new fitAddonGlobal.FitAddon();
            term.loadAddon(this._fitAddon);
            requestAnimationFrame(() => {
                try {
                    this._fitAddon?.fit();
                    if (this._state.term && this._state.term.cols > 0 && this._state.term.rows > 0) {
                        this._config.onResize?.(this._state.term.cols, this._state.term.rows);
                    }
                }
                catch (_) { }
            });
            this._ro = new ResizeObserver(() => {
                try {
                    this._fitAddon?.fit();
                    if (this._state.term && this._state.term.cols > 0 && this._state.term.rows > 0) {
                        this._config.onResize?.(this._state.term.cols, this._state.term.rows);
                    }
                }
                catch (_) { }
            });
            this._ro.observe(termDiv);
        }
        // biome-ignore lint/suspicious/noExplicitAny: window global access
        const webLinksAddonGlobal = window.WebLinksAddon ?? globalThis.WebLinksAddon;
        if (webLinksAddonGlobal) {
            try {
                term.loadAddon(new webLinksAddonGlobal.WebLinksAddon());
            }
            catch (_) { }
        }
        // Forward keyboard input to WS when hijacked or in open mode.
        term.onData((data) => {
            if (!this._state.ws || this._state.ws.readyState !== WebSocket.OPEN) {
                nudgeReconnect(this._state, this._handlers);
                return;
            }
            if (this._state.inputMode !== "open" && !this._state.hijackedByMe)
                return;
            // Show activity indicator (no local echo — let server echo drive the display).
            this._showActivityIndicator();
            wsSend(this._state, { type: "input", data });
        });
        return term;
    }
    /** Show activity indicator with the configured style (reuses DOM reference and timeout). */
    _showActivityIndicator() {
        if (!this._statusDotElement) {
            this._statusDotElement = this._q("dot");
        }
        const dot = this._statusDotElement;
        if (!dot)
            return;
        if (this._activityFlashTimer)
            clearTimeout(this._activityFlashTimer);
        dot.classList.add("activity-flash");
        this._activityFlashTimer = setTimeout(() => {
            dot.classList.remove("activity-flash");
            this._activityFlashTimer = null;
        }, 200);
    }
    _buildMobileKeys() {
        const container = this._q("mobilekeys");
        if (!container)
            return;
        for (const { label, data } of MOBILE_KEYS) {
            const btn = document.createElement("button");
            btn.className = "mkey";
            btn.textContent = label;
            btn.addEventListener("click", () => {
                if (this._state.inputMode !== "open" && !this._state.hijackedByMe)
                    return;
                if (!this._state.ws || this._state.ws.readyState !== WebSocket.OPEN)
                    return;
                this._showActivityIndicator();
                wsSend(this._state, { type: "input", data });
            });
            container.appendChild(btn);
        }
    }
    // ── Message dispatch ──────────────────────────────────────────────────────
    _handleMessage(msg) {
        const state = this._state;
        switch (msg.type) {
            case "term":
                state.workerOnline = true;
                if (msg.data) {
                    try {
                        this._ensureTerm().write(msg.data);
                    }
                    catch (_) { }
                }
                break;
            case "snapshot": {
                stopReconnectAnim(state);
                state.workerOnline = true;
                const promptDetected = msg.prompt_detected;
                const promptId = promptDetected?.prompt_id;
                this._setPromptId(promptId ?? "");
                try {
                    const t = this._ensureTerm();
                    // Use ANSI soft reset (DECSTR) + clear screen — preserves scrollback buffer.
                    // Avoids t.reset() which destroys scrollback and breaks scroll indicators.
                    t.write("[!p[2J[H");
                    t.write((msg.screen ?? "").replace(/\n/g, "\r\n"));
                }
                catch (_) { }
                break;
            }
            case "analysis": {
                const pre = this._q("analysistext");
                if (pre) {
                    pre.textContent = msg.formatted ?? "(no analysis)";
                    const details = this._q("analysis");
                    if (details)
                        details.open = true;
                }
                break;
            }
            case "hello": {
                state.canHijack = !!msg.can_hijack;
                state.hijacked = !!msg.hijacked;
                state.hijackedByMe = !!msg.hijacked_by_me;
                state.workerOnline = !!msg.worker_online;
                const inputMode = msg.input_mode;
                if (inputMode)
                    state.inputMode = inputMode;
                const caps = msg.capabilities;
                state.hijackControl =
                    msg.hijack_control ?? caps?.hijack_control ?? "ws";
                const stepSupported = msg.hijack_step_supported ?? caps?.hijack_step_supported;
                state.hijackStepSupported = stepSupported !== false;
                const resumeToken = msg.resume_token;
                if (resumeToken) {
                    state.resumeToken = resumeToken;
                    saveResumeToken(state, resumeToken);
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
                state.hijacked = !!msg.hijacked;
                state.hijackedByMe = msg.owner === "me";
                if (!state.hijackedByMe)
                    state.restHijackId = null;
                const hsInputMode = msg.input_mode;
                if (hsInputMode)
                    state.inputMode = hsInputMode;
                if (state.hijackedByMe) {
                    startHeartbeat(state);
                }
                else {
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
                const changedMode = msg.input_mode;
                if (changedMode)
                    state.inputMode = changedMode;
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
                }
                else {
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
                this._config.onPresenceMessage?.(msg);
                break;
        }
    }
    // ── UI State ──────────────────────────────────────────────────────────────
    _getEffectiveUxMode() {
        const mode = this._config.approvalUxMode;
        if (mode === "auto") {
            return this._config.role === "admin" ? "modal" : "statusbar";
        }
        return mode;
    }
    _showApprovalUI() {
        if (!this._pendingApproval || !this._root)
            return;
        this._hideApprovalUI();
        const mode = this._getEffectiveUxMode();
        const el = document.createElement("div");
        this._approvalElement = el;
        el.className = approvalElementClass(mode);
        const isAdmin = this._config.role === "admin";
        el.innerHTML =
            mode === "modal"
                ? buildApprovalModalHtml({ uid: this._uid, command: this._pendingApproval.command, isAdmin })
                : buildApprovalStatusBarHtml({ uid: this._uid });
        this._root.appendChild(el);
        this._startApprovalTimer();
        if (this._config.role === "admin") {
            this._q("approve")?.addEventListener("click", () => this._resolveApproval("approve"));
            this._q("reject")?.addEventListener("click", () => this._resolveApproval("reject"));
        }
    }
    _hideApprovalUI() {
        if (this._approvalTimer) {
            clearInterval(this._approvalTimer);
            this._approvalTimer = null;
        }
        if (this._approvalElement) {
            this._approvalElement.parentNode?.removeChild(this._approvalElement);
            this._approvalElement = null;
        }
    }
    _startApprovalTimer() {
        const update = () => {
            if (!this._pendingApproval)
                return;
            const remaining = computeRemainingSeconds(this._pendingApproval.expiresAt);
            const el = this._q("approval-timer");
            if (el)
                el.textContent = String(remaining);
            if (remaining <= 0)
                this._hideApprovalUI();
        };
        update();
        this._approvalTimer = setInterval(update, 1000);
    }
    async _resolveApproval(action) {
        if (!this._pendingApproval)
            return;
        const btn = this._q(action);
        if (btn) {
            btn.disabled = true;
            btn.textContent = "Sending...";
        }
        try {
            const url = `${location.origin}/api/approvals/${this._pendingApproval.id}/${action}`;
            const resp = await fetch(url, { method: "POST" });
            if (!resp.ok) {
                throw new Error(`Failed to ${action}: ${resp.statusText}`);
            }
        }
        catch (err) {
            console.error(err);
            if (btn) {
                btn.disabled = false;
                btn.textContent = action.charAt(0).toUpperCase() + action.slice(1);
            }
            this._setStatus("bad", `Failed to ${action} command`);
        }
    }
    _setStatus(level, text) {
        const dot = this._q("dot");
        const txt = this._q("statustext");
        if (dot) {
            dot.className = `hijack-status-dot ${level}`;
        }
        if (txt)
            txt.textContent = text;
    }
    _updateStatus() {
        const state = this._state;
        const connected = !!(state.ws && state.ws.readyState === WebSocket.OPEN);
        if (!connected) {
            this._setStatus("bad", "Disconnected"); // red — WS down
        }
        else if (!state.workerOnline) {
            if (state.wakingTimedOut) {
                this._setStatus("bad", "Offline"); // red — worker never came online
            }
            else {
                this._setStatus("warn", "Waking…"); // orange — connected but worker not yet online
            }
        }
        else if (state.hijackedByMe) {
            this._setStatus("live", "Hijacked (you)");
        }
        else if (state.hijacked) {
            this._setStatus("warn", "Hijacked (other)");
        }
        else if (state.inputMode === "open") {
            this._setStatus("live", "Connected (shared)");
        }
        else {
            this._setStatus("live", "Connected (watching)");
        }
        const canInput = state.hijackedByMe || state.inputMode === "open";
        if (this._config.showInput) {
            const row = this._q("inputrow");
            if (row)
                row.classList.toggle("visible", connected && canInput);
        }
        if (this._config.mobileKeys) {
            const mkRow = this._q("mobilekeys");
            if (mkRow)
                mkRow.classList.toggle("visible", connected && canInput && this._mobileKeysVisible);
        }
    }
    _updateButtons() {
        const state = this._state;
        const connected = !!(state.ws && state.ws.readyState === WebSocket.OPEN);
        const hijackBtn = this._q("hijack");
        const stepBtn = this._q("step");
        const releaseBtn = this._q("release");
        const resyncBtn = this._q("resync");
        const analyzeBtn = this._q("analyze");
        if (!connected) {
            for (const b of [hijackBtn, stepBtn, releaseBtn, resyncBtn, analyzeBtn]) {
                if (b)
                    b.disabled = true;
            }
            return;
        }
        const isOpen = state.inputMode === "open";
        const hideHijack = isOpen || !state.canHijack;
        if (hijackBtn)
            hijackBtn.disabled = hideHijack || state.hijacked || !state.workerOnline;
        if (stepBtn)
            stepBtn.disabled = hideHijack || !state.hijackedByMe || !state.hijackStepSupported;
        if (releaseBtn)
            releaseBtn.disabled = hideHijack || !state.hijackedByMe;
        if (resyncBtn)
            resyncBtn.disabled = !state.workerOnline;
        if (analyzeBtn)
            analyzeBtn.disabled = hideHijack || !state.hijackedByMe;
        if (hijackBtn)
            hijackBtn.style.display = hideHijack ? "none" : "";
        if (stepBtn)
            stepBtn.style.display = hideHijack ? "none" : "";
        if (releaseBtn)
            releaseBtn.style.display = hideHijack ? "none" : "";
    }
    _setPromptId(id) {
        const el = this._q("prompt");
        if (el)
            el.textContent = id ? `prompt: ${id}` : "";
    }
    // ── Event Binding ─────────────────────────────────────────────────────────
    _bindEvents() {
        const state = this._state;
        this._q("hijack")?.addEventListener("click", () => {
            if (!state.ws || state.ws.readyState !== WebSocket.OPEN)
                return;
            if (state.hijackControl === "rest") {
                restHijack(state, "acquire", { owner: "dashboard", lease_s: 60 })
                    .then((data) => {
                    if (data && typeof data.hijack_id === "string")
                        state.restHijackId = data.hijack_id;
                })
                    .finally(() => wsSend(state, { type: "snapshot_req" }));
                return;
            }
            wsSend(state, { type: "hijack_request" });
        });
        this._q("step")?.addEventListener("click", () => {
            if (!state.ws || state.ws.readyState !== WebSocket.OPEN)
                return;
            if (!state.hijackedByMe)
                return;
            if (state.hijackControl === "rest") {
                restHijack(state, "step").catch(() => { });
            }
            else {
                wsSend(state, { type: "hijack_step" });
            }
            // Request snapshot + analysis shortly after the worker acts.
            for (const ms of [250, 1000]) {
                setTimeout(() => {
                    if (state.ws && state.ws.readyState === WebSocket.OPEN)
                        wsSend(state, { type: "snapshot_req" });
                }, ms);
            }
            for (const ms of [450, 1200]) {
                setTimeout(() => {
                    if (state.ws && state.ws.readyState === WebSocket.OPEN)
                        wsSend(state, { type: "analyze_req" });
                }, ms);
            }
        });
        this._q("release")?.addEventListener("click", () => {
            if (!state.ws || state.ws.readyState !== WebSocket.OPEN)
                return;
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
            if (!state.ws || state.ws.readyState !== WebSocket.OPEN)
                return;
            wsSend(state, { type: "snapshot_req" });
        });
        this._q("analyze")?.addEventListener("click", () => {
            if (!state.ws || state.ws.readyState !== WebSocket.OPEN)
                return;
            if (!state.hijackedByMe)
                return;
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
        const inputField = this._q("inputfield");
        const inputSend = this._q("inputsend");
        if (inputField) {
            const doSend = () => {
                const raw = inputField.value;
                if (!raw || (state.inputMode !== "open" && !state.hijackedByMe))
                    return;
                if (!state.ws || state.ws.readyState !== WebSocket.OPEN)
                    return;
                // Unescape \\r → \r, \\n → \n, \\t → \t, \\e → ESC
                const data = raw.replace(/\\r/g, "\r").replace(/\\n/g, "\n").replace(/\\t/g, "\t").replace(/\\e/g, "\x1b");
                this._showActivityIndicator();
                wsSend(state, { type: "input", data });
                inputField.value = "";
                try {
                    this._ensureTerm().focus();
                }
                catch (_) { }
            };
            inputField.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    e.preventDefault();
                    doSend();
                }
            });
            if (inputSend)
                inputSend.addEventListener("click", doSend);
        }
    }
}
// ── Global exposure for CDN / script-tag use ──────────────────────────────────
// biome-ignore lint/suspicious/noExplicitAny: window global access
if (typeof window !== "undefined")
    window.ProvideHijack = ProvideHijack;
// Re-exported so external code that imported the helper from hijack.ts directly
// still works after the WS-lifecycle extraction.
export { escapeHijackHtml };
