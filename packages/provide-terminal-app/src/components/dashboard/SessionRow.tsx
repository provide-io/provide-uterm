//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AppBootstrap, SessionSummary } from "../../api/types";
import { useDashboardStore } from "../../stores/dashboardStore";
import { StatusBadge } from "../common/StatusBadge";
import styles from "./SessionRow.module.css";

interface SessionRowProps {
  session: SessionSummary;
  bootstrap: AppBootstrap;
}

export function SessionRow({ session, bootstrap }: SessionRowProps) {
  const restart = useDashboardStore((s) => s.restart);
  const appPath = bootstrap.app_path;
  const hasError = session.lastError !== null;
  const dotColor = hasError
    ? "var(--danger)"
    : session.connected
      ? "var(--success)"
      : "var(--text-tertiary)";

  return (
    <div className={styles.row} data-dimmed={!session.connected && !hasError ? "" : undefined}>
      <div
        className={styles.dot}
        style={{
          background: dotColor,
          boxShadow: session.connected || hasError ? `0 0 6px ${dotColor}` : undefined,
        }}
      />
      <div className={styles.info}>
        <div className={styles.nameRow}>
          <span className={styles.name}>{session.displayName}</span>
          <StatusBadge tone={session.connected ? "ok" : hasError ? "error" : "neutral"}>
            {session.connected ? "Live" : hasError ? "Error" : "Stopped"}
          </StatusBadge>
          <StatusBadge tone="neutral">{session.connectorType}</StatusBadge>
          {session.recordingEnabled && <StatusBadge tone="warning">rec</StatusBadge>}
          {session.visibility !== "public" && <StatusBadge tone="neutral">{session.visibility}</StatusBadge>}
        </div>
        <div className={styles.meta}>
          {session.inputMode === "open" ? "Shared" : "Exclusive"} mode
          {hasError && ` · ${session.lastError}`}
        </div>
      </div>
      <div className={styles.actions}>
        <a className="action-link" href={`${appPath}/operator/${encodeURIComponent(session.sessionId)}`}>Operate</a>
        {session.connected && (
          <a className="action-link" href={`${appPath}/session/${encodeURIComponent(session.sessionId)}`}>View</a>
        )}
        {session.recordingAvailable && (
          <a className="action-link" href={`${appPath}/replay/${encodeURIComponent(session.sessionId)}`}>Replay</a>
        )}
        <button type="button" className="btn-sm"
          onClick={() => void restart(session.sessionId)}>
          Restart
        </button>
      </div>
    </div>
  );
}
