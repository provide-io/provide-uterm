//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useState } from "react";
import type { HttpExchangeEntry } from "../../api/types";

function humanSize(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

function decodeBody(entry: { body_b64?: string; body_truncated?: boolean; body_binary?: boolean; body_size: number }): string {
  if (entry.body_b64) {
    try { return atob(entry.body_b64); } catch { return "(decode error)"; }
  }
  if (entry.body_truncated) return `(truncated, ${humanSize(entry.body_size)})`;
  if (entry.body_binary) return `(binary, ${humanSize(entry.body_size)})`;
  return "";
}

function HeaderTable({ headers }: { headers: Record<string, string> }) {
  const entries = Object.entries(headers);
  if (entries.length === 0) return <em>none</em>;
  return (
    <div style={{ fontSize: 12, fontFamily: "monospace" }}>
      {entries.map(([k, v]) => (
        <div key={k}><b>{k}:</b> {v}</div>
      ))}
    </div>
  );
}

interface InspectDetailProps {
  exchange: HttpExchangeEntry | null;
  onAction: (id: string, action: string, headers?: Record<string, string>, bodyB64?: string) => void;
}

export function InspectDetail({ exchange, onAction }: InspectDetailProps) {
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState("");
  const [editHeaders, setEditHeaders] = useState<Array<[string, string]>>([]);

  if (!exchange) {
    return <div style={{ padding: 24, color: "var(--text-tertiary)" }}>Select a request to view details</div>;
  }

  const { request: r, response: res } = exchange;
  const paused = exchange.intercepted && !exchange.interceptResolved && !res;
  const reqBody = decodeBody(r);
  const resBody = res ? decodeBody(res) : "";

  function startModify() {
    let body = "";
    if (r.body_b64) { try { body = atob(r.body_b64); } catch { body = ""; } }
    setEditBody(body);
    setEditHeaders(Object.entries(r.headers));
    setEditing(true);
  }

  function sendModified() {
    const hdrs: Record<string, string> = {};
    for (const [k, v] of editHeaders) {
      if (k) hdrs[k] = v;
    }
    onAction(r.id, "modify", hdrs, btoa(editBody));
    setEditing(false);
  }

  return (
    <div style={{ padding: 16, overflowY: "auto", flex: 1 }}>
      <h3 style={{ margin: "0 0 8px", fontSize: 14 }}>{r.method} {r.url}</h3>

      {paused && (
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <span style={{ fontSize: 11, padding: "2px 8px", background: "var(--bg-warning)", borderRadius: 4 }}>PAUSED</span>
          <button type="button" onClick={() => onAction(r.id, "forward")}>Forward</button>
          <button type="button" onClick={() => onAction(r.id, "drop")}>Drop</button>
          <button type="button" onClick={startModify}>Modify &amp; Forward</button>
        </div>
      )}
      {exchange.interceptAction && (
        <div style={{ marginBottom: 12 }}>
          <span style={{ fontSize: 11, padding: "2px 8px", background: "var(--bg-info)", borderRadius: 4 }}>
            {exchange.interceptAction}
          </span>
        </div>
      )}

      {res && (
        <div style={{ marginBottom: 12, fontSize: 13, fontWeight: 600 }}>
          {res.status} {res.status_text} — {res.duration_ms.toFixed(0)}ms
        </div>
      )}
      {!res && <div style={{ marginBottom: 12, fontSize: 13, color: "var(--text-tertiary)" }}>Pending…</div>}

      <h4 style={{ margin: "12px 0 4px", fontSize: 12 }}>Request Headers</h4>
      <HeaderTable headers={r.headers} />
      {reqBody && (
        <>
          <h4 style={{ margin: "12px 0 4px", fontSize: 12 }}>Request Body</h4>
          <pre style={{ fontSize: 11, whiteSpace: "pre-wrap", background: "var(--bg-secondary)", padding: 8, borderRadius: 4 }}>{reqBody}</pre>
        </>
      )}

      {res && (
        <>
          <h4 style={{ margin: "12px 0 4px", fontSize: 12 }}>Response Headers</h4>
          <HeaderTable headers={res.headers} />
          {resBody && (
            <>
              <h4 style={{ margin: "12px 0 4px", fontSize: 12 }}>Response Body</h4>
              <pre style={{ fontSize: 11, whiteSpace: "pre-wrap", background: "var(--bg-secondary)", padding: 8, borderRadius: 4 }}>{resBody}</pre>
            </>
          )}
        </>
      )}

      {editing && (
        <div style={{ marginTop: 16, padding: 12, border: "1px solid var(--border-primary)", borderRadius: 6 }}>
          <h4 style={{ margin: "0 0 8px", fontSize: 12 }}>Modify Request</h4>
          {editHeaders.map(([k, v], i) => (
            <div key={`${k}:${v}`} style={{ display: "flex", gap: 4, marginBottom: 4 }}>
              <input value={k} onChange={(e) => { const h = [...editHeaders]; h[i] = [e.target.value, h[i]?.[1] ?? ""]; setEditHeaders(h); }} style={{ flex: 1, fontSize: 12 }} />
              <input value={v} onChange={(e) => { const h = [...editHeaders]; h[i] = [h[i]?.[0] ?? "", e.target.value]; setEditHeaders(h); }} style={{ flex: 2, fontSize: 12 }} />
            </div>
          ))}
          <h4 style={{ margin: "8px 0 4px", fontSize: 12 }}>Body</h4>
          <textarea rows={6} value={editBody} onChange={(e) => setEditBody(e.target.value)} style={{ width: "100%", fontSize: 12, fontFamily: "monospace" }} />
          <button type="button" onClick={sendModified} style={{ marginTop: 8 }}>Send Modified</button>
        </div>
      )}
    </div>
  );
}
