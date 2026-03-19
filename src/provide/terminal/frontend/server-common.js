//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
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
    const response = await fetch(path, init);
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
