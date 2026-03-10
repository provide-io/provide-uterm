import { loadDashboardState, summarizeSessions } from "../state.js";
const _POLL_INTERVAL_MS = 10_000;
function escapeHtml(value) {
    return value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
function sectionMarkup(title, sessions, appPath, { showConnectCta = false } = {}) {
    const safeTitle = escapeHtml(title);
    const safeAppPath = escapeHtml(appPath);
    if (sessions.length === 0) {
        const cta = showConnectCta
            ? `<a class="btn primary" href="${safeAppPath}/connect">Quick Connect</a>`
            : "";
        return `
      <section class="card stack">
        <div class="section-heading"><h2>${safeTitle}</h2><div class="small">No sessions.</div></div>
        ${cta}
      </section>
    `;
    }
    return `
    <section class="card stack">
      <div class="section-heading"><h2>${safeTitle}</h2><div class="small">${sessions.length} session(s)</div></div>
      <div class="session-list">
        ${sessions
        .map((session) => `
          <article class="session-card ${session.connected ? "live" : ""} ${session.lastError ? "error" : ""}">
            <div class="session-header">
              <div>
                <div class="session-title">${escapeHtml(session.displayName)}</div>
                <div class="small">${escapeHtml(session.sessionId)} • ${escapeHtml(session.connectorType)}</div>
              </div>
              <span class="status-chip ${session.connected ? "ok" : session.lastError ? "error" : "info"}">${session.connected ? "Live" : session.lastError ? "Error" : "Stopped"}</span>
            </div>
            <div class="small">Mode: ${escapeHtml(session.inputMode)} • State: ${escapeHtml(session.lifecycleState)}</div>
            <div class="toolbar">
              <a class="btn" href="${safeAppPath}/operator/${encodeURIComponent(session.sessionId)}">Operator</a>
              <a class="btn" href="${safeAppPath}/session/${encodeURIComponent(session.sessionId)}">User view</a>
              <a class="btn" href="${safeAppPath}/replay/${encodeURIComponent(session.sessionId)}">Replay</a>
            </div>
          </article>
        `)
        .join("")}
      </div>
    </section>
  `;
}
async function refreshSessions(content, status, appPath) {
    try {
        const sessions = await loadDashboardState();
        const groups = summarizeSessions(sessions);
        status.className = "status-chip ok";
        status.textContent = `${sessions.length} session(s) · ${new Date().toLocaleTimeString()}`;
        content.innerHTML = [
            sectionMarkup("Running", groups.running, appPath, { showConnectCta: true }),
            sectionMarkup("Stopped", groups.stopped, appPath),
            sectionMarkup("Degraded", groups.degraded, appPath),
        ].join("");
    }
    catch (error) {
        status.className = "status-chip error";
        status.textContent = `Failed to load sessions: ${String(error)}`;
        content.innerHTML = `<section class="card"><div class="small">Unable to load session state.</div></section>`;
    }
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
          <a class="btn primary" href="${safeAppPath}/connect">Quick Connect</a>
          <button id="dashboard-refresh" class="btn">Refresh</button>
        </div>
        <div id="dashboard-status" class="status-chip info">Loading sessions…</div>
      </section>
      <div id="dashboard-content" class="page"></div>
    </div>
  `;
    const status = root.querySelector("#dashboard-status");
    const content = root.querySelector("#dashboard-content");
    const refreshBtn = root.querySelector("#dashboard-refresh");
    if (!status || !content || !refreshBtn)
        throw new Error("dashboard shell is incomplete");
    await refreshSessions(content, status, bootstrap.app_path);
    refreshBtn.addEventListener("click", () => {
        status.className = "status-chip info";
        status.textContent = "Refreshing…";
        void refreshSessions(content, status, bootstrap.app_path);
    });
    const timer = setInterval(() => {
        void refreshSessions(content, status, bootstrap.app_path);
    }, _POLL_INTERVAL_MS);
    // Stop polling when navigating away (handles SPA-style page transitions).
    document.addEventListener("visibilitychange", () => {
        if (document.hidden)
            clearInterval(timer);
    }, { once: true });
}
