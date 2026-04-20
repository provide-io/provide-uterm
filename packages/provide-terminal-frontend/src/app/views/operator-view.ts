//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { clearRuntime, loadOperatorWorkspaceState, requestAnalysis, switchSessionMode } from "../state.js";
import type { AppBootstrap, SessionMode } from "../types.js";
import { mountHijackWidget } from "../widgets/hijack-widget-host.js";
import { renderAppHeader } from "./app-header.js";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function infoRow(label: string, value: unknown): string {
  return `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
    <span class="small">${esc(label)}</span>
    <span style="font-weight:600">${esc(String(value ?? "\u2014"))}</span>
  </div>`;
}

function renderTags(tags: string[]): string {
  if (!tags.length) return '<span class="small">none</span>';
  return `<div class="tag-list">${tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>`;
}

function sidebarHtml(s: SessionSummary | null, appPath: string, sessionId: string): string {
  const shareQuery = window.location.search || "";
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
        <a class="btn" href="${esc(appPath)}/replay/${encodeURIComponent(sessionId)}${esc(shareQuery)}">View replay</a>
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

export async function renderOperator(root: HTMLElement, bootstrap: AppBootstrap): Promise<void> {
  if (!bootstrap.session_id) throw new Error("operator bootstrap missing session_id");
  const sessionId = bootstrap.session_id;
  const safeTitle = escapeHtml(bootstrap.title);
  const safeAppPath = escapeHtml(bootstrap.app_path);
  root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "operator")}
      <div class="layout">
        <section class="card stack">
        <div class="small">Operator Console</div>
        <h1>${safeTitle}</h1>
        <div class="toolbar">
          <button class="btn" id="btn-refresh">Refresh</button>
          <button class="btn" id="btn-open">Shared Mode</button>
          <button class="btn" id="btn-hijack">Exclusive Mode</button>
          <button class="btn" id="btn-clear">Clear</button>
          <button class="btn" id="btn-analyze">Analyze</button>
          <a class="btn" id="btn-replay" href="${safeAppPath}/replay/${encodeURIComponent(sessionId)}">Replay</a>
        </div>
        <div id="operator-status" class="status-chip info">Loading operator workspace…</div>
        <pre class="small" id="meta"></pre>
        </section>
        <section class="card">
          <div id="widget"></div>
        </section>
      </div>
    </div>
  `;
  const status = root.querySelector<HTMLElement>("#operator-status");
  const meta = root.querySelector<HTMLElement>("#meta");
  const widget = root.querySelector<HTMLElement>("#widget");
  if (!status || !meta || !widget) throw new Error("operator shell is incomplete");

  const refresh = async (): Promise<void> => {
    const state = await loadOperatorWorkspaceState(sessionId);
    status.className = `status-chip ${state.status.tone}`;
    status.textContent = state.status.text;
    meta.textContent = JSON.stringify(
      {
        session: state.session.summary,
        snapshot: { prompt_id: state.session.snapshotPromptId },
      },
      null,
      2,
    );
  };

  try {
    await refresh();
    const widgetState = mountHijackWidget(widget, sessionId, "operator");
    if (!widgetState.mounted) {
      status.className = "status-chip error";
      status.textContent = widgetState.error ?? "Widget mount failed";
    }
  } catch (error) {
    status.className = "status-chip error";
    status.textContent = `Operator workspace failed to load: ${String(error)}`;
  }

  root.querySelector<HTMLButtonElement>("#btn-refresh")?.addEventListener("click", () => void refresh());
  root.querySelector<HTMLButtonElement>("#btn-open")?.addEventListener("click", () => {
    void applyMode(sessionId, "open", status, meta);
  });
  root.querySelector<HTMLButtonElement>("#btn-hijack")?.addEventListener("click", () => {
    void applyMode(sessionId, "hijack", status, meta);
  });
  root.querySelector<HTMLButtonElement>("#btn-clear")?.addEventListener("click", () => {
    void clearRuntime(sessionId)
      .then((state) => {
        status.className = "status-chip ok";
        status.textContent = "Session cleared.";
        meta.textContent = JSON.stringify(
          { session: state.summary, snapshot: { prompt_id: state.snapshotPromptId } },
          null,
          2,
        );
      })
      .catch((error) => {
        status.className = "status-chip error";
        status.textContent = `Clear failed: ${String(error)}`;
      });
  });
  root.querySelector<HTMLButtonElement>("#btn-analyze")?.addEventListener("click", () => {
    void requestAnalysis(sessionId)
      .then((analysis) => {
        window.alert(analysis);
      })
      .catch((error) => {
        status.className = "status-chip error";
        status.textContent = `Analyze failed: ${String(error)}`;
      });
  });
}
