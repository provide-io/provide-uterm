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

async function applyMode(sessionId: string, mode: SessionMode, status: HTMLElement, meta: HTMLElement): Promise<void> {
  const state = await switchSessionMode(sessionId, mode);
  status.className = "status-chip ok";
  status.textContent = `${state.summary.displayName} is now in ${state.summary.inputMode} mode.`;
  meta.textContent = JSON.stringify(
    {
      session: state.summary,
      snapshot: { prompt_id: state.snapshotPromptId },
    },
    null,
    2,
  );
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
