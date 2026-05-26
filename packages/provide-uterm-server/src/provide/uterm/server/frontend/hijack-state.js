//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
export class HijackState {
    constructor(config, workerId, wsDecoder) {
        this.ws = null;
        this.term = null;
        this.heartbeatTimer = null;
        this.reconnectTimer = null;
        this.reconnectAnimTimer = null;
        this.reconnectAttempt = 0;
        this.wakingTimer = null;
        this.wakingTimedOut = false;
        this.hijacked = false;
        this.hijackedByMe = false;
        this.canHijack = false;
        this.workerOnline = false;
        this.inputMode = "hijack";
        this.hijackControl = "ws";
        this.hijackStepSupported = true;
        this.restHijackId = null;
        this.resumeToken = null;
        /**
         * Server-confirmed role from the `hello` frame, or null until received.
         * Preferred over `config.role` (constructor input) for UX decisions, since
         * the server is authoritative on the actual role the connection holds.
         */
        this.serverRole = null;
        /** One-shot guard so the wss+token audit warning only fires once per state. */
        this._wssTokenWarned = false;
        this.config = config;
        this.workerId = workerId;
        this.wsDecoder = wsDecoder;
    }
}
