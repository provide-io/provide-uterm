//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { _DLE, _STX } from "../../hijack-codec.js";
import { getShareToken, requireElement } from "../../server-common.js";
import type { AppBootstrap, HttpExchangeEntry, HttpRequestEntry, HttpResponseEntry } from "../types.js";
import { renderAppHeader } from "./app-header.js";

interface InspectState {
  exchanges: HttpExchangeEntry[];
  selected: string | null;
  ws: WebSocket | null;
}

function statusClass(status: number): string {
  if (status >= 500) return "s5xx";
  if (status >= 400) return "s4xx";
  if (status >= 300) return "s3xx";
  return "s2xx";
}

function humanSize(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

function decodeControlFrames(raw: string): Array<Record<string, unknown>> {
  const frames: Array<Record<string, unknown>> = [];
  let pos = 0;
  while (pos < raw.length) {
    const dleIdx = raw.indexOf(_DLE, pos);
    if (dleIdx === -1) break;
    if (dleIdx + 1 < raw.length && raw[dleIdx + 1] === _STX) {
      // Control frame: DLE STX [8 hex len] : [json]
      const header = raw.substring(dleIdx + 2, dleIdx + 10);
      if (header.length === 8 && raw[dleIdx + 10] === ":") {
        const len = parseInt(header, 16);
        const json = raw.substring(dleIdx + 11, dleIdx + 11 + len);
        try {
          frames.push(JSON.parse(json) as Record<string, unknown>);
        } catch {
          /* skip malformed */
        }
        pos = dleIdx + 11 + len;
        continue;
      }
    }
    pos = dleIdx + 1;
  }
  return frames;
}

function renderRow(ex: HttpExchangeEntry): string {
  const r = ex.request;
  const res = ex.response;
  const status = res
    ? `<span class="status ${statusClass(res.status)}">${res.status}</span>`
    : '<span class="status">…</span>';
  const dur = res ? `${res.duration_ms.toFixed(0)}ms` : "—";
  const size = res ? humanSize(res.body_size) : "—";
  return `<div class="inspect-row" data-id="${r.id}">
    <span class="method">${r.method}</span>
    <span class="url" title="${r.url}">${r.url}</span>
    ${status}
    <span class="duration">${dur}</span>
    <span class="size">${size}</span>
  </div>`;
}

function renderDetail(ex: HttpExchangeEntry): string {
  const r = ex.request;
  const res = ex.response;
  const reqHeaders = Object.entries(r.headers)
    .map(([k, v]) => `<div><b>${k}:</b> ${v}</div>`)
    .join("");
  const resHeaders = res
    ? Object.entries(res.headers)
        .map(([k, v]) => `<div><b>${k}:</b> ${v}</div>`)
        .join("")
    : "";

  let reqBody = "";
  if (r.body_b64) {
    try {
      reqBody = atob(r.body_b64);
    } catch {
      reqBody = "(decode error)";
    }
  } else if (r.body_truncated) {
    reqBody = `(truncated, ${humanSize(r.body_size)})`;
  } else if (r.body_binary) {
    reqBody = `(binary, ${humanSize(r.body_size)})`;
  }

  let resBody = "";
  if (res?.body_b64) {
    try {
      resBody = atob(res.body_b64);
    } catch {
      resBody = "(decode error)";
    }
  } else if (res?.body_truncated) {
    resBody = `(truncated, ${humanSize(res.body_size)})`;
  } else if (res?.body_binary) {
    resBody = `(binary, ${humanSize(res.body_size)})`;
  }

  return `
    <div class="inspect-detail-section">
      <h3>${r.method} ${r.url}</h3>
      ${res ? `<div class="inspect-status ${statusClass(res.status)}">${res.status} ${res.status_text} — ${res.duration_ms.toFixed(0)}ms</div>` : '<div class="inspect-status">Pending…</div>'}
    </div>
    <div class="inspect-detail-section">
      <h4>Request Headers</h4>
      <div class="inspect-headers">${reqHeaders || "<em>none</em>"}</div>
      ${reqBody ? `<h4>Request Body</h4><pre class="inspect-body">${reqBody}</pre>` : ""}
    </div>
    ${
      res
        ? `<div class="inspect-detail-section">
      <h4>Response Headers</h4>
      <div class="inspect-headers">${resHeaders || "<em>none</em>"}</div>
      ${resBody ? `<h4>Response Body</h4><pre class="inspect-body">${resBody}</pre>` : ""}
    </div>`
        : ""
    }
  `;
}

export async function renderInspect(root: HTMLElement, bootstrap: AppBootstrap): Promise<void> {
  if (!bootstrap.session_id) throw new Error("inspect bootstrap missing session_id");
  const sessionId = bootstrap.session_id;

  root.innerHTML = `
    <div class="page inspect-page">
      ${renderAppHeader(bootstrap, "inspect")}
      <div class="inspect-layout">
        <div class="inspect-toolbar">
          <select id="inspect-method-filter">
            <option value="">All Methods</option>
            <option>GET</option><option>POST</option><option>PUT</option>
            <option>DELETE</option><option>PATCH</option><option>HEAD</option><option>OPTIONS</option>
          </select>
          <input id="inspect-url-filter" type="text" placeholder="Filter URL..." />
          <span id="inspect-count">0 requests</span>
          <span id="inspect-status" class="status-chip info">Connecting…</span>
        </div>
        <div class="inspect-split">
          <div id="inspect-list" class="inspect-list"></div>
          <div id="inspect-detail" class="inspect-detail">
            <div class="inspect-empty">Select a request to view details</div>
          </div>
        </div>
      </div>
    </div>
  `;

  const listEl = requireElement<HTMLElement>("#inspect-list", root);
  const detailEl = requireElement<HTMLElement>("#inspect-detail", root);
  const countEl = requireElement<HTMLElement>("#inspect-count", root);
  const statusEl = requireElement<HTMLElement>("#inspect-status", root);
  const methodFilter = requireElement<HTMLSelectElement>("#inspect-method-filter", root);
  const urlFilter = requireElement<HTMLInputElement>("#inspect-url-filter", root);

  const state: InspectState = { exchanges: [], selected: null, ws: null };

  function updateList(): void {
    const mf = methodFilter.value;
    const uf = urlFilter.value.toLowerCase();
    const filtered = state.exchanges.filter((ex) => {
      if (mf && ex.request.method !== mf) return false;
      if (uf && !ex.request.url.toLowerCase().includes(uf)) return false;
      return true;
    });
    listEl.innerHTML = filtered.map(renderRow).join("");
    countEl.textContent = `${filtered.length} request${filtered.length !== 1 ? "s" : ""}`;

    // Re-select
    if (state.selected) {
      const sel = listEl.querySelector(`[data-id="${state.selected}"]`);
      sel?.classList.add("selected");
    }
  }

  function showDetail(id: string): void {
    state.selected = id;
    const ex = state.exchanges.find((e) => e.id === id);
    if (ex) {
      detailEl.innerHTML = renderDetail(ex);
    }
    listEl.querySelectorAll(".inspect-row").forEach((el) => {
      el.classList.toggle("selected", el.getAttribute("data-id") === id);
    });
  }

  listEl.addEventListener("click", (e) => {
    const row = (e.target as HTMLElement).closest(".inspect-row");
    if (row) showDetail(row.getAttribute("data-id") ?? "");
  });
  methodFilter.addEventListener("change", updateList);
  urlFilter.addEventListener("input", updateList);

  // Connect WebSocket
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let wsUrl = `${proto}//${location.host}/ws/browser/${encodeURIComponent(sessionId)}/term`;
  const shareToken = getShareToken();
  if (shareToken) {
    wsUrl += `?token=${encodeURIComponent(shareToken)}`;
  }

  const ws = new WebSocket(wsUrl);
  state.ws = ws;

  ws.addEventListener("open", () => {
    statusEl.className = "status-chip ok";
    statusEl.textContent = "Connected";
  });

  ws.addEventListener("close", () => {
    statusEl.className = "status-chip error";
    statusEl.textContent = "Disconnected";
  });

  ws.addEventListener("message", (event) => {
    if (typeof event.data !== "string") return;
    const frames = decodeControlFrames(event.data);
    for (const frame of frames) {
      if (frame._channel !== "http") continue;
      const type = frame.type as string;

      if (type === "http_req") {
        const req = frame as unknown as HttpRequestEntry;
        state.exchanges.push({ id: req.id, request: req, response: null });
        updateList();
        // Auto-scroll to bottom
        listEl.scrollTop = listEl.scrollHeight;
      } else if (type === "http_res") {
        const res = frame as unknown as HttpResponseEntry;
        const ex = state.exchanges.find((e) => e.id === res.id);
        if (ex) {
          ex.response = res;
          updateList();
          if (state.selected === res.id) showDetail(res.id);
        }
      }
    }
  });
}
