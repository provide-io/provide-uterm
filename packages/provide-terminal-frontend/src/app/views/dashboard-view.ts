//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { deleteProfile, deleteSession, fetchProfiles, restartSession } from "../api.js";
import { loadDashboardState, summarizeSessions } from "../state.js";
import type { AppBootstrap, ConnectionProfile, SessionSummary } from "../types.js";
import { renderAppHeader } from "./app-header.js";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function profilesSectionMarkup(profiles: ConnectionProfile[], appPath: string): string {
  const safeAppPath = escapeHtml(appPath);
  if (profiles.length === 0) {
    return `
      <section class="card stack">
        <div class="section-heading">
          <h2>Profiles</h2>
          <div class="small">No saved profiles. <a href="${safeAppPath}/connect">Connect</a> and save one.</div>
        </div>
      </section>
    `;
  }
  return `
    <section class="card stack">
      <div class="section-heading"><h2>Profiles</h2><div class="small">${profiles.length} saved</div></div>
      <div class="session-list">
        ${profiles
          .map(
            (p) => `
          <article class="session-card" data-profile-id="${escapeHtml(p.profile_id)}">
            <div class="session-header">
              <div>
                <span class="session-title">${escapeHtml(p.name)}</span>
                <div class="small">${escapeHtml(p.connector_type)}${p.host ? ` · ${escapeHtml(p.host)}${p.port ? `:${p.port}` : ""}` : ""}</div>
              </div>
              <div class="session-badges">
                ${p.visibility === "shared" ? `<span class="badge badge-visibility">shared</span>` : ""}
              </div>
            </div>
            ${p.tags.length > 0 ? `<div class="tag-list">${p.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>` : ""}
            <div class="toolbar">
              <a class="btn" href="${safeAppPath}/connect?profile=${encodeURIComponent(p.profile_id)}">Connect</a>
              <button class="btn btn-delete-profile" data-profile-id="${escapeHtml(p.profile_id)}">Delete</button>
            </div>
          </article>
        `,
          )
          .join("")}
      </div>
    </section>
  `;
}

function sectionMarkup(title: string, sessions: SessionSummary[], appPath: string): string {
  const safeTitle = escapeHtml(title);
  const safeAppPath = escapeHtml(appPath);
  if (sessions.length === 0) {
    return `
      <section class="card stack">
        <div class="section-heading"><h2>${safeTitle}</h2><div class="small">No sessions.</div></div>
      </section>
    `;
  }
  return `
    <section class="card stack">
      <div class="section-heading"><h2>${safeTitle}</h2><div class="small">${sessions.length} session(s)</div></div>
      <div class="session-list">
        ${sessions
          .map(
            (session) => `
          <article class="session-card ${session.connected ? "live" : ""} ${session.lastError ? "error" : ""}" data-session-id="${escapeHtml(session.sessionId)}">
            <div class="session-header">
              <div>
                <a class="session-title" href="${safeAppPath}/operator/${encodeURIComponent(session.sessionId)}">${escapeHtml(session.displayName)}</a>
                <div class="small">${escapeHtml(session.sessionId)} • ${escapeHtml(session.connectorType)}</div>
              </div>
              <div class="session-badges">
                ${session.visibility !== "public" ? `<span class="badge badge-visibility">${escapeHtml(session.visibility)}</span>` : ""}
                ${session.recordingEnabled ? `<span class="badge badge-rec">⏺ rec</span>` : ""}
                ${session.recordingAvailable && !session.recordingEnabled ? `<span class="badge badge-rec-avail">⏺ saved</span>` : ""}
                <span class="status-chip ${session.connected ? "ok" : session.lastError ? "error" : "info"}">${
                  session.connected ? "Live" : session.lastError ? "Error" : "Stopped"
                }</span>
              </div>
            </div>
            ${
              session.tags.length > 0
                ? `<div class="tag-list">${session.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>`
                : ""
            }
            <div class="toolbar">
              <a class="btn" href="${safeAppPath}/operator/${encodeURIComponent(session.sessionId)}">Control</a>
              <a class="btn" href="${safeAppPath}/session/${encodeURIComponent(session.sessionId)}">Watch</a>
              <a class="btn" href="${safeAppPath}/replay/${encodeURIComponent(session.sessionId)}">Replay</a>
              <button class="btn btn-restart" data-session-id="${escapeHtml(session.sessionId)}">Restart</button>
              <button class="btn btn-delete" data-session-id="${escapeHtml(session.sessionId)}">Delete</button>
            </div>
          </article>
        `,
          )
          .join("")}
      </div>
    </section>
  `;
}

export async function renderDashboard(root: HTMLElement, bootstrap: AppBootstrap): Promise<void> {
  const safeTitle = escapeHtml(bootstrap.title);
  root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "dashboard")}
      <section class="card stack">
        <h1>${safeTitle}</h1>
        <div class="toolbar">
          <button id="dashboard-refresh" class="btn">Refresh</button>
        </div>
        <div id="dashboard-status" class="status-chip info">Loading…</div>
      </section>
      <div id="dashboard-content" class="page"></div>
    </div>
  `;
  const status = root.querySelector<HTMLElement>("#dashboard-status");
  const content = root.querySelector<HTMLElement>("#dashboard-content");
  if (!status || !content) throw new Error("dashboard shell is incomplete");

  async function loadAll(statusEl: HTMLElement, contentEl: HTMLElement): Promise<void> {
    try {
      const [sessions, profiles] = await Promise.all([loadDashboardState(), fetchProfiles()]);
      const groups = summarizeSessions(sessions);
      statusEl.className = "status-chip ok";
      statusEl.textContent = `${sessions.length} session(s) · ${profiles.length} profile(s)`;
      contentEl.innerHTML = [
        profilesSectionMarkup(profiles, bootstrap.app_path),
        sectionMarkup("Active", groups.running, bootstrap.app_path),
        sectionMarkup("Idle", groups.stopped, bootstrap.app_path),
        sectionMarkup("Error", groups.degraded, bootstrap.app_path),
      ].join("");
    } catch (error) {
      statusEl.className = "status-chip error";
      statusEl.textContent = `Dashboard failed to load: ${String(error)}`;
      contentEl.innerHTML = `<section class="card"><div class="small">Unable to load dashboard state.</div></section>`;
    }
  }

  root.querySelector<HTMLButtonElement>("#dashboard-refresh")?.addEventListener("click", () => {
    void loadAll(status, content);
  });

  content.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;

    const deleteProfileBtn = target.closest<HTMLButtonElement>(".btn-delete-profile");
    if (deleteProfileBtn) {
      const pid = deleteProfileBtn.dataset.profileId;
      if (!pid) return;
      if (!window.confirm(`Delete profile? This cannot be undone.`)) return;
      deleteProfileBtn.disabled = true;
      deleteProfileBtn.textContent = "…";
      void deleteProfile(pid)
        .then(() => loadAll(status, content))
        .catch((err: unknown) => {
          deleteProfileBtn.disabled = false;
          deleteProfileBtn.textContent = "Delete";
          status.className = "status-chip error";
          status.textContent = `Delete failed: ${String(err)}`;
        });
      return;
    }

    const restartBtn = target.closest<HTMLButtonElement>(".btn-restart");
    if (restartBtn) {
      const sid = restartBtn.dataset.sessionId;
      if (!sid) return;
      restartBtn.disabled = true;
      restartBtn.textContent = "…";
      void restartSession(sid)
        .then(() => loadAll(status, content))
        .catch((err: unknown) => {
          restartBtn.disabled = false;
          restartBtn.textContent = "Restart";
          status.className = "status-chip error";
          status.textContent = `Restart failed: ${String(err)}`;
        });
      return;
    }

    const deleteBtn = target.closest<HTMLButtonElement>(".btn-delete");
    if (deleteBtn) {
      const sid = deleteBtn.dataset.sessionId;
      if (!sid) return;
      if (!window.confirm(`Delete session "${sid}"? This cannot be undone.`)) return;
      deleteBtn.disabled = true;
      deleteBtn.textContent = "…";
      void deleteSession(sid)
        .then(() => loadAll(status, content))
        .catch((err: unknown) => {
          deleteBtn.disabled = false;
          deleteBtn.textContent = "Delete";
          status.className = "status-chip error";
          status.textContent = `Delete failed: ${String(err)}`;
        });
    }
  });

  await loadAll(status, content);
}
