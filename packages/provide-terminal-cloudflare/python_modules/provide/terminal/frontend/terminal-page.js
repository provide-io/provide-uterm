"use strict";
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
function sanitizeWorkerId(value) {
    if (!value)
        return "demo";
    return /^[A-Za-z0-9_-]{1,64}$/.test(value) ? value : "demo";
}
function resolveWsPath() {
    const params = new URLSearchParams(window.location.search);
    const workerId = sanitizeWorkerId(params.get("worker_id"));
    const roleParam = params.get("role");
    const role = roleParam === "browser" ? "browser" : "raw";
    return `/ws/${role}/${workerId}/term`;
}
function initTerminalPage() {
    const container = document.getElementById("app");
    if (!(container instanceof HTMLElement)) {
        throw new Error("Missing #app container");
    }
    const TerminalWidget = window.ProvideTerminal;
    if (typeof TerminalWidget !== "function") {
        throw new Error("ProvideTerminal is not available");
    }
    window.demoTerminal = new TerminalWidget(container, {
        wsUrl: resolveWsPath(),
        title: "Provide Terminal Cloudflare",
    });
}
initTerminalPage();
