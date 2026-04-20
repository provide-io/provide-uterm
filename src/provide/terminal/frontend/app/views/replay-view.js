import { loadReplayState } from "../state.js";
import { renderAppHeader } from "./app-header.js";
function renderEntryList(entries, index) {
    if (entries.length === 0) {
        return `<div class="small">No entries match the current filter.</div>`;
    }
    return entries
        .map((entry, entryIndex) => `
        <button class="btn replay-entry${entryIndex === index ? " primary" : ""}" data-index="${entryIndex}">
          ${entry.event}${entry.ts !== null ? ` • ${new Date(entry.ts * 1000).toLocaleTimeString()}` : ""}
        </button>
      `)
        .join("");
}
function updateReplayUi(root, state) {
    const meta = root.querySelector("#replay-meta");
    const list = root.querySelector("#replay-list");
    const screen = root.querySelector("#replay-screen");
    const json = root.querySelector("#replay-json");
    const scrubber = root.querySelector("#replay-scrubber");
    if (!meta || !list || !screen || !json || !scrubber)
        return;
    const entry = state.entries[state.index] ?? null;
    meta.textContent = JSON.stringify({
        total: state.total,
        index: state.entries.length === 0 ? 0 : state.index + 1,
        filter: state.filter || "all",
        limit: state.limit,
        status: state.status.text,
    }, null, 2);
    list.innerHTML = renderEntryList(state.entries, state.index);
    screen.textContent = entry?.screen || "";
    json.textContent = JSON.stringify(entry ? { event: entry.event, data: entry.payload, ts: entry.ts } : {}, null, 2);
    scrubber.disabled = state.entries.length === 0;
    scrubber.max = String(Math.max(0, state.entries.length - 1));
    scrubber.value = String(Math.min(state.index, Math.max(0, state.entries.length - 1)));
}
export async function renderReplay(root, bootstrap) {
    if (!bootstrap.session_id)
        throw new Error("replay bootstrap missing session_id");
    const sessionId = bootstrap.session_id;
    root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "replay")}
      <section class="card stack">
        <div class="small">Replay</div>
        <h1>${bootstrap.title} (${sessionId})</h1>
        <div class="toolbar">
          <button class="btn" id="btn-load">Reload</button>
          <button class="btn" id="btn-prev">Prev</button>
          <button class="btn" id="btn-next">Next</button>
          <button class="btn" id="btn-first">First</button>
          <button class="btn" id="btn-last">Last</button>
          <label class="small toolbar-label">Event
            <select id="replay-filter" class="toolbar-select">
              <option value="">All</option>
              <option value="read">read</option>
              <option value="send">send</option>
              <option value="runtime_started">runtime_started</option>
              <option value="runtime_error">runtime_error</option>
              <option value="log_start">log_start</option>
              <option value="log_stop">log_stop</option>
            </select>
          </label>
          <label class="small toolbar-label">Limit
            <select id="replay-limit" class="toolbar-select">
              <option value="25">25</option>
              <option value="100">100</option>
              <option value="200" selected>200</option>
            </select>
          </label>
          <a class="btn" href="/api/sessions/${encodeURIComponent(sessionId)}/recording/download">Download JSONL</a>
        </div>
        <input id="replay-scrubber" type="range" min="0" max="0" value="0" disabled>
        <pre class="small" id="replay-meta">Loading recording…</pre>
      </section>
      <div class="layout">
        <section class="card"><div class="small">Timeline</div><div id="replay-list" class="scroll-list"></div></section>
        <div class="stack">
          <section class="card"><div class="small">Rendered screen</div><pre id="replay-screen" class="pre-screen-half"></pre></section>
          <section class="card"><div class="small">Entry payload</div><pre id="replay-json" class="pre-json-half"></pre></section>
        </div>
      </div>
    </div>
  `;
    const filter = root.querySelector("#replay-filter");
    const limit = root.querySelector("#replay-limit");
    const scrubber = root.querySelector("#replay-scrubber");
    if (!filter || !limit || !scrubber)
        throw new Error("replay shell is incomplete");
    let state = await loadReplayState(sessionId, filter.value, Number(limit.value));
    updateReplayUi(root, state);
    const reload = async () => {
        state = await loadReplayState(sessionId, filter.value, Number(limit.value));
        updateReplayUi(root, state);
    };
    const clampIndex = (nextIndex) => {
        state = { ...state, index: Math.max(0, Math.min(nextIndex, Math.max(0, state.entries.length - 1))) };
        updateReplayUi(root, state);
    };
    root.querySelector("#btn-load")?.addEventListener("click", () => void reload());
    root.querySelector("#btn-prev")?.addEventListener("click", () => clampIndex(state.index - 1));
    root.querySelector("#btn-next")?.addEventListener("click", () => clampIndex(state.index + 1));
    root.querySelector("#btn-first")?.addEventListener("click", () => clampIndex(0));
    root
        .querySelector("#btn-last")
        ?.addEventListener("click", () => clampIndex(state.entries.length - 1));
    filter.addEventListener("change", () => void reload());
    limit.addEventListener("change", () => void reload());
    scrubber.addEventListener("input", () => clampIndex(Number(scrubber.value)));
    root.querySelector("#replay-list")?.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement))
            return;
        const index = target.getAttribute("data-index");
        if (index === null)
            return;
        clampIndex(Number(index));
    });
}
