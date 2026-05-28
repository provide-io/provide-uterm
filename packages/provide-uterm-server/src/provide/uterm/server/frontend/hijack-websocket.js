//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * WebSocket lifecycle helpers extracted from ProvideHijack. These free
 * functions operate on a HijackState container and call back into the owning
 * widget via HijackHandlers for UI updates and message dispatch. Keeping these
 * out of the main class keeps hijack.ts focused on DOM/event wiring.
 */
import { _RECONNECT_ANIM_FRAMES, encodeWsFrame } from "./hijack-codec.js";
// Backoff schedule for reconnect attempts (seconds). Final value reused for all
// subsequent attempts.
const RECONNECT_DELAYS = [1, 2, 5, 10, 30];
const WAKING_TIMEOUT_MS = 10000;
const RECONNECT_ANIM_INTERVAL_MS = 80;
export function resolveWsUrl(state) {
    const { config } = state;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = config.wsUrl;
    if (url) {
        if (url.startsWith("/"))
            return `${proto}//${location.host}${url}`;
        return url;
    }
    const workerId = encodeURIComponent(config.workerId ?? "default");
    const prefix = config.wsPathPrefix;
    return `${proto}//${location.host}${prefix}/${workerId}/term`;
}
export function resolveHijackApiBase(state) {
    const workerId = encodeURIComponent(state.config.workerId ?? "default");
    return `/worker/${workerId}/hijack`;
}
export function withAuthToken(_state, path) {
    return path;
}
export function saveResumeToken(state, token) {
    try {
        sessionStorage.setItem(`uterm_resume_${state.workerId}`, token);
    }
    catch (_) { }
}
export function loadResumeToken(state) {
    try {
        return sessionStorage.getItem(`uterm_resume_${state.workerId}`);
    }
    catch (_) {
        return null;
    }
}
export async function restHijack(state, action, payload = {}) {
    const headers = { "content-type": "application/json" };
    const base = resolveHijackApiBase(state);
    let path;
    if (action === "acquire") {
        path = `${base}/acquire`;
    }
    else {
        if (!state.restHijackId)
            return null;
        path = `${base}/${encodeURIComponent(state.restHijackId)}/${action}`;
    }
    const resp = await fetch(withAuthToken(state, path), {
        method: "POST",
        credentials: "include",
        headers,
        body: JSON.stringify(payload),
    });
    if (!resp.ok)
        return null;
    return (await resp.json());
}
export function wsSend(state, obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(encodeWsFrame(obj));
    }
}
export function clearHeartbeat(state) {
    if (state.heartbeatTimer) {
        clearInterval(state.heartbeatTimer);
        state.heartbeatTimer = null;
    }
}
export function startHeartbeat(state) {
    clearHeartbeat(state);
    state.heartbeatTimer = setInterval(() => {
        if (!state.hijackedByMe)
            return;
        if (state.hijackControl === "rest") {
            restHijack(state, "heartbeat", { lease_s: 60 }).catch(() => { });
            return;
        }
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN)
            return;
        wsSend(state, { type: "heartbeat" });
    }, state.config.heartbeatInterval);
}
export function startReconnectAnim(state) {
    if (state.reconnectAnimTimer || !state.term)
        return;
    let i = 0;
    state.reconnectAnimTimer = setInterval(() => {
        if (!state.term)
            return;
        const ch = _RECONNECT_ANIM_FRAMES[i % _RECONNECT_ANIM_FRAMES.length] ?? "⠋";
        i++;
        try {
            state.term.write(`\x1b7\x1b[B\x1b[G\x1b[2;36m${ch}\x1b[0m\x1b8`);
        }
        catch (_) { }
    }, RECONNECT_ANIM_INTERVAL_MS);
}
export function stopReconnectAnim(state) {
    if (!state.reconnectAnimTimer)
        return;
    clearInterval(state.reconnectAnimTimer);
    state.reconnectAnimTimer = null;
    if (state.term) {
        try {
            state.term.write("\x1b7\x1b[B\x1b[G \x1b8");
        }
        catch (_) { }
    }
}
export function connectWs(state, handlers) {
    if (state.ws) {
        try {
            state.ws.close();
        }
        catch (_) { }
        state.ws = null;
    }
    // Do NOT reset _hijacked/_hijackedByMe here: the server confirms actual state
    // via 'hello'/'hijack_state' once the socket opens. Eager reset would briefly
    // re-enable the Hijack button even when another client holds the lock.
    // (State is correctly reset to false in ws.onclose when the connection drops.)
    let ws;
    try {
        ws = new WebSocket(resolveWsUrl(state));
    }
    catch (e) {
        const err = e instanceof Error ? e : new Error(String(e));
        handlers.setStatus("bad", `Failed: ${err.message}`);
        return;
    }
    state.ws = ws;
    state.wsDecoder.reset();
    ws.onopen = () => {
        if (ws !== state.ws)
            return; // stale handler — a newer socket replaced this one
        stopReconnectAnim(state);
        state.reconnectAttempt = 0;
        state.wakingTimedOut = false;
        if (state.wakingTimer)
            clearTimeout(state.wakingTimer);
        state.wakingTimer = setTimeout(() => {
            state.wakingTimer = null;
            if (!state.workerOnline && state.ws?.readyState === WebSocket.OPEN) {
                state.wakingTimedOut = true;
                handlers.updateStatus();
            }
        }, WAKING_TIMEOUT_MS);
        handlers.setStatus("warn", "Waking…");
        handlers.updateButtons();
        const storedToken = state.resumeToken ?? loadResumeToken(state);
        if (storedToken) {
            wsSend(state, { type: "resume", token: storedToken });
        }
        wsSend(state, { type: "snapshot_req" });
        startHeartbeat(state);
    };
    ws.onmessage = (e) => {
        try {
            const frames = state.wsDecoder.feed(typeof e.data === "string" ? e.data : String(e.data));
            for (const frame of frames) {
                const msg = frame.type === "data" ? { type: "term", data: frame.data } : frame.control;
                if (msg.type) {
                    handlers.handleMessage(msg);
                }
            }
        }
        catch (_) {
            handlers.setStatus("bad", "Protocol error");
            try {
                ws.close();
            }
            catch (_) { }
            return;
        }
    };
    ws.onclose = () => {
        if (ws !== state.ws)
            return; // stale handler from a replaced socket
        clearHeartbeat(state);
        if (state.wakingTimer) {
            clearTimeout(state.wakingTimer);
            state.wakingTimer = null;
        }
        state.wakingTimedOut = false;
        state.hijacked = false;
        state.hijackedByMe = false;
        state.canHijack = false;
        state.workerOnline = false;
        state.inputMode = "hijack";
        state.hijackControl = "ws";
        state.hijackStepSupported = true;
        state.restHijackId = null;
        // Do NOT clear resumeToken — needed for session resumption on reconnect.
        handlers.updateStatus();
        handlers.updateButtons();
        state.ws = null;
        scheduleReconnect(state, handlers);
    };
    ws.onerror = () => {
        try {
            ws.close();
        }
        catch (_) { }
    };
}
export function scheduleReconnect(state, handlers) {
    if (state.reconnectTimer)
        return;
    const attempt = state.reconnectAttempt;
    const delaySec = RECONNECT_DELAYS[Math.min(attempt, RECONNECT_DELAYS.length - 1)] ?? 30;
    state.reconnectAttempt = attempt + 1;
    handlers.setStatus("bad", `Reconnecting in ${delaySec}s…`);
    startReconnectAnim(state);
    state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        connectWs(state, handlers);
    }, delaySec * 1000);
}
/** Cancel any pending backoff timer and reconnect immediately. */
export function nudgeReconnect(state, handlers) {
    if (state.ws && state.ws.readyState === WebSocket.CONNECTING)
        return;
    if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
        connectWs(state, handlers);
    }
}
