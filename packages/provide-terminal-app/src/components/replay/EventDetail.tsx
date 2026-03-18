//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { RecordingEntryView } from "../../api/types";

interface EventDetailProps {
  entry: RecordingEntryView | null;
}

export function EventDetail({ entry }: EventDetailProps) {
  if (!entry) {
    return (
      <div className="detail-pad text-muted">
        No event selected.
      </div>
    );
  }

  const ts = entry.ts != null ? formatTimestamp(entry.ts) : "—";
  const dataStr = JSON.stringify(entry.payload, null, 2);
  const bytes = entry.payload.data
    ? String(entry.payload.data).length
    : dataStr.length;

  return (
    <>
      <div className="detail-header">
        <div className="section-label mb-4">
          Event detail
        </div>
        <div className="detail-title">{entry.event}</div>
      </div>
      <div className="detail-body">
        <div className="meta-list">
          <MetaRow label="Timestamp" value={ts} />
          <MetaRow label="Event" value={entry.event} />
          <MetaRow label="Bytes" value={String(bytes)} />
        </div>
        <div className="code-box text-mono code-box-scroll">
          {dataStr}
        </div>
      </div>
    </>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="meta-row">
      <span className="text-muted">{label}</span>
      <span className="text-mono">{value}</span>
    </div>
  );
}

function formatTimestamp(ts: number): string {
  const totalSec = Math.floor(ts);
  const ms = Math.round((ts - totalSec) * 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}
