//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AppBootstrap } from "../../api/types";
import { useDashboardStore } from "../../stores/dashboardStore";
import { SessionRow } from "./SessionRow";

interface SessionListProps {
  bootstrap: AppBootstrap;
  filter: string;
}

export function SessionList({ bootstrap, filter }: SessionListProps) {
  const sessions = useDashboardStore((s) => s.sessions);
  const lowerFilter = filter.toLowerCase();

  const filtered = lowerFilter
    ? sessions.filter(
        (s) =>
          s.displayName.toLowerCase().includes(lowerFilter) ||
          s.sessionId.toLowerCase().includes(lowerFilter) ||
          s.connectorType.toLowerCase().includes(lowerFilter) ||
          s.tags.some((t) => t.toLowerCase().includes(lowerFilter)),
      )
    : sessions;

  // Sort: errors first, then connected, then stopped
  const sorted = [...filtered].sort((a, b) => {
    const aScore = a.lastError ? 0 : a.connected ? 1 : 2;
    const bScore = b.lastError ? 0 : b.connected ? 1 : 2;
    return aScore - bScore;
  });

  if (sorted.length === 0) {
    return <div className="empty-state">No sessions found.</div>;
  }

  return (
    <div className="session-stack">
      {sorted.map((session) => (
        <SessionRow key={session.sessionId} session={session} bootstrap={bootstrap} />
      ))}
    </div>
  );
}
