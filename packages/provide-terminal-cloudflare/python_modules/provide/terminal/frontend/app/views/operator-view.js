//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { deleteSession, restartSession } from "../api.js";
import { clearRuntime, loadOperatorWorkspaceState, requestAnalysis, switchSessionMode } from "../state.js";
import { mountHijackWidget } from "../widgets/hijack-widget-host.js";
import { renderAppHeader } from "./app-header.js";
function esc(value) {
    return value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
function infoRow(label, value) {
    return `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
    <span class="small">${esc(label)}</span>
    <span style="font-weight:600">${esc(String(value ?? "\u2014"))}</span>
  </div>`;
}
function renderTags(tags) {
    if (!tags.length)
        return '<span class="small">none</span>';
    return `<div class="tag-list">${tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>`;
}
function sidebarHtml(s, appPath, sessionId) {
    const name = s?.displayName ?? sessionId;
    const isOpen = s?.inputMode === "open";
    const liveBadge = s?.connected
        ? '<span class="badge" style="background:rgba(49,196,141,0.15);border:1px solid rgba(49,196,141,0.4);color:#b7f7dd">Live</span>'
        : '<span class="badge badge-visibility">Offline</span>';
    return `<section class="card stack">
    <div class="sidebar-section">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
        <div>
          <div class="session-title">${esc(name)}</div>
          <div class="small" style="margin-top:2px">Control</div>
        </div>
        ${liveBadge}
      </div>
      <div id="operator-status" class="status-chip info">Loading\u2026</div>
    </div>

    <div class="sidebar-section">
      <div class="small" style="text-transform:uppercase;letter-spacing:0.06em">Input Mode</div>
      <div class="toolbar" style="margin:0">
        <button class="btn${isOpen ? " primary" : ""}" id="btn-open">${isOpen ? "\u2713 " : ""}Shared</button>
        <button class="btn${!isOpen ? " primary" : ""}" id="btn-hijack">${!isOpen ? "\u2713 " : ""}Exclusive</button>
      </div>
      <div class="small">${isOpen ? "All operators can type." : "Only the hijack holder can type."}</div>
    </div>

    <div class="sidebar-section">
      <div class="small" style="text-transform:uppercase;letter-spacing:0.06em">Actions</div>
      <div class="toolbar" style="margin:0">
        <a class="btn" href="${esc(appPath)}/replay/${encodeURIComponent(sessionId)}">View replay</a>
        <button class="btn" id="btn-clear">Clear runtime</button>
        <button class="btn" id="btn-restart">Restart session</button>
        <button class="btn" id="btn-delete">Delete session</button>
      </div>
    </div>

    <div class="sidebar-section">
      <details>
        <summary class="small" style="cursor:pointer;user-select:none;text-transform:uppercase;letter-spacing:0.06em">Advanced</summary>
        <div class="toolbar" style="margin:6px 0 0">
          <button class="btn" id="btn-analyze">Analyze screen</button>
        </div>
        <pre id="analysis-result" class="small" style="display:none;margin-top:8px;white-space:pre-wrap;background:var(--panel2);border-radius:8px;padding:10px"></pre>
        <div class="small" style="margin-top:4px">AI-readable description of current terminal contents.</div>
      </details>
    </div>

    <div class="sidebar-section">
      <details>
        <summary class="small" style="cursor:pointer;user-select:none;text-transform:uppercase;letter-spacing:0.06em">Session Info</summary>
        <div style="margin-top:6px">
          ${infoRow("Connector", s?.connectorType)}
          ${infoRow("State", s?.lifecycleState)}
          ${infoRow("Owner", s?.owner)}
          ${infoRow("Visibility", s?.visibility)}
          ${infoRow("Auto-start", s?.autoStart ? "yes" : "no")}
        </div>
        <div style="margin-top:6px">
          <div class="small" style="text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px">Tags</div>
          ${renderTags(s?.tags ?? [])}
        </div>
      </details>
    </div>
  </section>`;
}
export async function renderOperator(root, bootstrap) {
    if (!bootstrap.session_id)
        throw new Error("operator bootstrap missing session_id");
    const sessionId = bootstrap.session_id;
    const appPath = esc(bootstrap.app_path);
    root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "operator")}
      <div class="layout">
        ${sidebarHtml(null, appPath, sessionId)}
        <section class="card"><div id="widget"></div></section>
      </div>
    </div>`;
    const widget = root.querySelector("#widget");
    if (!widget)
        throw new Error("operator shell is incomplete");
    // Wire all sidebar buttons — called after each sidebar re-render.
    const wire = () => {
        root.querySelector("#btn-open")?.addEventListener("click", () => {
            void switchSessionMode(sessionId, "open").then(() => void refresh());
        });
        root.querySelector("#btn-hijack")?.addEventListener("click", () => {
            void switchSessionMode(sessionId, "hijack").then(() => void refresh());
        });
        root.querySelector("#btn-clear")?.addEventListener("click", () => {
            if (!window.confirm("Clear the runtime state for this session?"))
                return;
            void clearRuntime(sessionId)
                .then(() => void refresh())
                .catch((e) => setStatus("error", `Clear failed: ${String(e)}`));
        });
        root.querySelector("#btn-analyze")?.addEventListener("click", () => {
            void requestAnalysis(sessionId)
                .then((a) => {
                const el = root.querySelector("#analysis-result");
                if (el) {
                    el.textContent = a;
                    el.style.display = "block";
                }
            })
                .catch((e) => setStatus("error", `Analyze failed: ${String(e)}`));
        });
        root.querySelector("#btn-restart")?.addEventListener("click", () => {
            if (!window.confirm("Restart this session? The current connection will be dropped."))
                return;
            void restartSession(sessionId)
                .then(() => void refresh())
                .catch((e) => setStatus("error", `Restart failed: ${String(e)}`));
        });
        root.querySelector("#btn-delete")?.addEventListener("click", () => {
            if (!window.confirm(`Delete session "${sessionId}"? This cannot be undone.`))
                return;
            void deleteSession(sessionId)
                .then(() => {
                window.location.href = `${bootstrap.app_path}/`;
            })
                .catch((e) => setStatus("error", `Delete failed: ${String(e)}`));
        });
    };
    const setStatus = (tone, text) => {
        const el = root.querySelector("#operator-status");
        if (el) {
            el.className = `status-chip ${tone}`;
            el.textContent = text;
        }
    };
    const refresh = async () => {
        const state = await loadOperatorWorkspaceState(sessionId);
        setStatus(state.status.tone, state.status.text);
        // Re-render sidebar with fresh data
        const sidebar = root.querySelector(".layout > section:first-child");
        if (sidebar) {
            const tmp = document.createElement("div");
            tmp.innerHTML = sidebarHtml(state.session.summary, appPath, sessionId);
            const next = tmp.querySelector("section");
            if (next) {
                sidebar.replaceWith(next);
                // Restore status text after re-render
                setStatus(state.status.tone, state.status.text);
                wire();
            }
        }
    };
    try {
        await refresh();
        const widgetState = mountHijackWidget(widget, sessionId, "operator");
        if (!widgetState.mounted) {
            setStatus("error", widgetState.error ?? "Widget mount failed");
        }
    }
    catch (error) {
        setStatus("error", `Operator workspace failed to load: ${String(error)}`);
    }
    wire();
}
