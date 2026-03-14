import { loadUserWorkspaceState } from "../state.js";
import { mountHijackWidget } from "../widgets/hijack-widget-host.js";
import { renderAppHeader } from "./app-header.js";
export async function renderSession(root, bootstrap) {
    if (!bootstrap.session_id)
        throw new Error("session bootstrap missing session_id");
    root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "session")}
      <div class="layout">
        <section class="card stack">
        <div class="small">Session View</div>
        <h1>${bootstrap.title}</h1>
        <div id="session-status" class="status-chip info">Loading session…</div>
        <div id="session-meta" class="small"></div>
        </section>
        <section class="card">
          <div id="widget"></div>
        </section>
      </div>
    </div>
  `;
    const status = root.querySelector("#session-status");
    const meta = root.querySelector("#session-meta");
    const widget = root.querySelector("#widget");
    if (!status || !meta || !widget)
        throw new Error("session shell is incomplete");
    try {
        const state = await loadUserWorkspaceState(bootstrap.session_id);
        status.className = `status-chip ${state.status.tone}`;
        status.textContent = state.status.text;
        meta.textContent = `Prompt: ${state.session.snapshotPromptId ?? "unknown"} • Mode: ${state.session.summary.inputMode}`;
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
