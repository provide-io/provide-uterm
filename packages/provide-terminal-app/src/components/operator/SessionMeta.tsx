//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { SessionSummary } from "../../api/types";

interface SessionMetaProps {
  summary: SessionSummary;
}

export function SessionMeta({ summary }: SessionMetaProps) {
  return (
    <>
      <div className="border-section">
        <div className="section-label">Session info</div>
        <div className="meta-list">
          <MetaRow label="Connector" value={summary.connectorType} />
          <MetaRow label="State" value={summary.lifecycleState} />
          <MetaRow label="Owner" value={summary.owner ?? "—"} />
          <MetaRow label="Visibility" value={summary.visibility} />
          <MetaRow
            label="Auto-start"
            value={summary.autoStart ? "yes" : "no"}
            valueColor={summary.autoStart ? "var(--text-success)" : undefined}
          />
        </div>
      </div>

      {summary.tags.length > 0 && (
        <div className="border-section">
          <div className="section-label">Tags</div>
          <div className="tag-list">
            {summary.tags.map((tag) => (
              <span key={tag} className="tag-pill">{tag}</span>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function MetaRow({ label, value, valueColor }: { label: string; value: string; valueColor?: string }) {
  return (
    <div className="meta-row">
      <span className="text-muted">{label}</span>
      <span style={{ color: valueColor }}>{value}</span>
    </div>
  );
}
