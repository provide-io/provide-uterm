//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { routeApp } from "./router.js";
function readBootstrap() {
    const script = document.getElementById("app-bootstrap");
    if (!(script instanceof HTMLScriptElement)) {
        throw new Error("Missing #app-bootstrap payload");
    }
    const parsed = JSON.parse(script.textContent || "{}");
    if (parsed.page_kind !== "dashboard" &&
        parsed.page_kind !== "session" &&
        parsed.page_kind !== "operator" &&
        parsed.page_kind !== "replay" &&
        parsed.page_kind !== "connect" &&
        parsed.page_kind !== "inspect") {
        throw new Error("Invalid page bootstrap");
    }
    if (typeof parsed.title !== "string" ||
        typeof parsed.app_path !== "string" ||
        typeof parsed.assets_path !== "string") {
        throw new Error("Incomplete page bootstrap");
    }
    return parsed;
}
export async function bootApp() {
    const root = document.getElementById("app-root");
    if (!(root instanceof HTMLElement)) {
        throw new Error("Missing #app-root");
    }
    const bootstrap = readBootstrap();
    // Share-token auth rides on the HttpOnly uterm_tunnel_{id} cookie set by
    // the page handler — the token is never exposed to JS.
    await routeApp(root, bootstrap);
}
