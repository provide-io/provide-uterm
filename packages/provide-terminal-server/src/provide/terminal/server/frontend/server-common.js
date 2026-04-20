//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * Module-level share token for tunnel session authentication.
 * Set from bootstrap JSON on page load; null when using cookie-based auth.
 * SECURITY: ephemeral credential — do not log or persist to storage.
 */
let _shareToken = null;
/** Store the share token for subsequent API/WS calls. Pass null for cookie mode. */
export function setShareToken(token) {
    _shareToken = typeof token === "string" && token.length > 0 ? token : null;
}
/** Return the current share token, or null if in cookie mode. */
export function getShareToken() {
    return _shareToken;
}
/** Append the share token as a query parameter if set (query transport mode). No-op in cookie mode. */
export function withShareToken(path) {
    if (_shareToken === null)
        return path;
    const separator = path.includes("?") ? "&" : "?";
    return `${path}${separator}token=${encodeURIComponent(_shareToken)}`;
}
export async function apiJson(path, method = "GET", body = null) {
    const init = {
        method,
        headers: {
            "Content-Type": "application/json",
        },
    };
    if (body !== null) {
        init.body = JSON.stringify(body);
    }
    const response = await fetch(withShareToken(path), init);
    if (!response.ok) {
        throw new Error(String(response.status));
    }
    return (await response.json());
}
export function requireElement(selector, root = document) {
    const element = root.querySelector(selector);
    if (element === null) {
        throw new Error(`Missing required element: ${selector}`);
    }
    return element;
}
export function readDataset(element, name) {
    const value = element.dataset[name];
    if (typeof value !== "string" || value.length === 0) {
        throw new Error(`Missing required data attribute: ${name}`);
    }
    return value;
}
export function readBooleanDataset(element, name) {
    return readDataset(element, name) === "true";
}
