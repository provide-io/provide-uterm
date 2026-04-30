//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect, useRef } from "react";
import type { HttpExchangeEntry } from "../../api/types";

function statusColor(status: number): string {
  if (status >= 500) return "var(--text-danger, #e55)";
  if (status >= 400) return "var(--text-warning, #ea3)";
  if (status >= 300) return "var(--text-info, #58f)";
  return "var(--text-success, #4b4)";
}

function humanSize(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

interface InspectListProps {
  exchanges: HttpExchangeEntry[];
  selected: string | null;
  onSelect: (id: string) => void;
}

export function InspectList({ exchanges, selected, onSelect }: InspectListProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(exchanges.length);

  useEffect(() => {
    if (exchanges.length > prevCountRef.current && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
    prevCountRef.current = exchanges.length;
  }, [exchanges.length]);

  if (exchanges.length === 0) {
    return <div style={{ padding: 24, color: "var(--text-tertiary)" }}>No requests captured yet.</div>;
  }

  return (
    <div ref={listRef} style={{ overflowY: "auto", flex: 1 }}>
      {exchanges.map((ex) => {
        const r = ex.request;
        const res = ex.response;
        const isSelected = ex.id === selected;
        const paused = ex.intercepted && !ex.interceptResolved && !res;
        return (
          <div
            key={ex.id}
            onClick={() => onSelect(ex.id)}
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              padding: "6px 12px",
              cursor: "pointer",
              background: isSelected ? "var(--bg-selected, rgba(100,140,255,0.12))" : "transparent",
              borderLeft: isSelected ? "3px solid var(--accent, #58f)" : "3px solid transparent",
              fontSize: 13,
            }}
          >
            <span style={{ fontWeight: 700, width: 56, flexShrink: 0 }}>{r.method}</span>
            <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={r.url}>
              {r.url}
            </span>
            {paused && (
              <span style={{ fontSize: 10, padding: "1px 6px", background: "var(--bg-warning)", borderRadius: 4 }}>
                PAUSED
              </span>
            )}
            {ex.interceptAction && (
              <span style={{ fontSize: 10, padding: "1px 6px", background: "var(--bg-info)", borderRadius: 4 }}>
                {ex.interceptAction}
              </span>
            )}
            {res ? (
              <span style={{ color: statusColor(res.status), fontWeight: 600, width: 36, textAlign: "right" }}>
                {res.status}
              </span>
            ) : (
              <span style={{ color: "var(--text-tertiary)", width: 36, textAlign: "right" }}>…</span>
            )}
            <span style={{ width: 56, textAlign: "right", color: "var(--text-secondary)", fontSize: 12 }}>
              {res ? `${res.duration_ms.toFixed(0)}ms` : "—"}
            </span>
            <span style={{ width: 48, textAlign: "right", color: "var(--text-secondary)", fontSize: 12 }}>
              {res ? humanSize(res.body_size) : "—"}
            </span>
          </div>
        );
      })}
    </div>
  );
}
