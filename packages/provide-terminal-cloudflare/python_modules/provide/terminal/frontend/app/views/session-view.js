//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { loadUserWorkspaceState } from "../state.js";
import { mountHijackWidget } from "../widgets/hijack-widget-host.js";
import { renderAppHeader } from "./app-header.js";
export async function renderSession(root, bootstrap) {
    if (!bootstrap.session_id)
        throw new Error("session bootstrap missing session_id");
    const safeAppPath = bootstrap.app_path
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "session")}
      <section class="card" style="padding:12px 18px">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <span class="session-title">${bootstrap.title}</span>
          <div id="session-status" class="status-chip info">Loading…</div>
          <a class="btn" style="margin-left:auto" href="${safeAppPath}/operator/${encodeURIComponent(bootstrap.session_id)}">→ Control</a>
        </div>
      </section>
      <section class="card">
        <div id="widget"></div>
      </section>
    </div>
  `;
    const status = root.querySelector("#session-status");
    const widget = root.querySelector("#widget");
    if (!status || !widget)
        throw new Error("session shell is incomplete");
    try {
        const state = await loadUserWorkspaceState(bootstrap.session_id);
        status.className = `status-chip ${state.status.tone}`;
        status.textContent = state.status.text;
        const widgetState = mountHijackWidget(widget, bootstrap.session_id, "user");
        if (!widgetState.mounted) {
            status.className = "status-chip error";
            status.textContent = widgetState.error ?? "Widget mount failed";
        }
    }
    catch (error) {
        status.className = "status-chip error";
        status.textContent = `Session failed to load: ${String(error)}`;
    }
}
