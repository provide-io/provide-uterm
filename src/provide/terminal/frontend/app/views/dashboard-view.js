import { restartSession } from "../api.js";
import { loadDashboardState, summarizeSessions } from "../state.js";
function escapeHtml(value) {
    return value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
function sectionMarkup(title, sessions, appPath) {
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
        .map((session) => `
          <article class="session-card ${session.connected ? "live" : ""} ${session.lastError ? "error" : ""}" data-session-id="${escapeHtml(session.sessionId)}">
            <div class="session-header">
              <div>
                <div class="session-title">${escapeHtml(session.displayName)}</div>
                <div class="small">${escapeHtml(session.sessionId)} • ${escapeHtml(session.connectorType)}</div>
              </div>
              <div class="session-badges">
                ${session.visibility !== "public" ? `<span class="badge badge-visibility">${escapeHtml(session.visibility)}</span>` : ""}
                ${session.recordingEnabled ? `<span class="badge badge-rec">⏺ rec</span>` : ""}
                ${session.recordingAvailable && !session.recordingEnabled ? `<span class="badge badge-rec-avail">⏺ saved</span>` : ""}
                <span class="status-chip ${session.connected ? "ok" : session.lastError ? "error" : "info"}">${session.connected ? "Live" : session.lastError ? "Error" : "Stopped"}</span>
              </div>
            </div>
            <div class="small">Mode: ${escapeHtml(session.inputMode)} • State: ${escapeHtml(session.lifecycleState)}</div>
            ${session.tags.length > 0
        ? `<div class="tag-list">${session.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>`
        : ""}
            <div class="toolbar">
              <a class="btn" href="${safeAppPath}/operator/${encodeURIComponent(session.sessionId)}">Operator</a>
              <a class="btn" href="${safeAppPath}/session/${encodeURIComponent(session.sessionId)}">User view</a>
              <a class="btn" href="${safeAppPath}/replay/${encodeURIComponent(session.sessionId)}">Replay</a>
              <button class="btn btn-restart" data-session-id="${escapeHtml(session.sessionId)}">Restart</button>
            </div>
          </article>
        `)
        .join("")}
      </div>
    </section>
  `;
}
export async function renderDashboard(root, bootstrap) {
    const safeTitle = escapeHtml(bootstrap.title);
    const safeAppPath = escapeHtml(bootstrap.app_path);
    root.innerHTML = `
    <div class="page">
      <section class="card stack">
        <div class="small">Reference implementation</div>
        <h1>${safeTitle}</h1>
        <div class="toolbar">
          <a class="btn" href="${safeAppPath}/connect">Quick Connect</a>
          <button id="dashboard-refresh" class="btn">Refresh</button>
        </div>
        <div id="dashboard-status" class="status-chip info">Loading sessions…</div>
      </section>
      <div id="dashboard-content" class="page"></div>
    </div>
  `;
    const status = root.querySelector("#dashboard-status");
    const content = root.querySelector("#dashboard-content");
    if (!status || !content)
        throw new Error("dashboard shell is incomplete");
    async function loadSessions(statusEl, contentEl) {
        try {
            const sessions = await loadDashboardState();
            const groups = summarizeSessions(sessions);
            statusEl.className = "status-chip ok";
            statusEl.textContent = `${sessions.length} session(s) loaded`;
            contentEl.innerHTML = [
                sectionMarkup("Running", groups.running, bootstrap.app_path),
                sectionMarkup("Stopped", groups.stopped, bootstrap.app_path),
                sectionMarkup("Degraded", groups.degraded, bootstrap.app_path),
            ].join("");
        }
        catch (error) {
            statusEl.className = "status-chip error";
            statusEl.textContent = `Dashboard failed to load: ${String(error)}`;
            contentEl.innerHTML = `<section class="card"><div class="small">Unable to load session state.</div></section>`;
        }
    }
    root.querySelector("#dashboard-refresh")?.addEventListener("click", () => {
        void loadSessions(status, content);
    });
    content.addEventListener("click", (e) => {
        const btn = e.target.closest(".btn-restart");
        if (!btn)
            return;
        const sid = btn.dataset.sessionId;
        if (!sid)
            return;
        btn.disabled = true;
        btn.textContent = "…";
        void restartSession(sid)
            .then(() => loadSessions(status, content))
            .catch((err) => {
            btn.disabled = false;
            btn.textContent = "Restart";
            status.className = "status-chip error";
            status.textContent = `Restart failed: ${String(err)}`;
        });
    });
    await loadSessions(status, content);
}
