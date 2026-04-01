//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AppBootstrap } from "../types.js";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export type AppHeaderTab = "dashboard" | "connect" | "session" | "operator" | "replay" | "inspect";

export function renderAppHeader(bootstrap: AppBootstrap, active: AppHeaderTab): string {
  const safeAppPath = escapeHtml(bootstrap.app_path);
  const isDashboard = active === "dashboard";
  const isConnect = active === "connect";
  return `
    <header class="app-header card">
      <nav class="app-nav">
        <a class="app-nav-link${isDashboard ? " active" : ""}" href="${safeAppPath}/">Dashboard</a>
        <a class="app-nav-link${isConnect ? " active" : ""}" href="${safeAppPath}/connect">Quick Connect</a>
      </nav>
    </header>
  `;
}
