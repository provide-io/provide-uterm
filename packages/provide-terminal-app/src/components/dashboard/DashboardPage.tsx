//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect } from "react";
import type { AppBootstrap } from "../../api/types";
import { useDashboardStore } from "../../stores/dashboardStore";
import { FilterInput } from "../common/FilterInput";
import { MetricCard } from "../common/MetricCard";
import { AppHeader } from "../layout/AppHeader";
import { PageShell } from "../layout/PageShell";
import { SessionList } from "./SessionList";
import styles from "./DashboardPage.module.css";

interface DashboardPageProps {
  bootstrap: AppBootstrap;
}

export function DashboardPage({ bootstrap }: DashboardPageProps) {
  const { sessions, filter, loading, error, setFilter, refresh } = useDashboardStore();

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const live = sessions.filter((s) => s.connected && s.lifecycleState === "running");
  const errors = sessions.filter((s) => s.lifecycleState === "error" || s.lastError !== null);
  const recording = sessions.filter((s) => s.recordingEnabled);
  const healthy = errors.length === 0;

  return (
    <PageShell>
      <AppHeader
        bootstrap={bootstrap}
        right={
          <a href={`${bootstrap.app_path}/connect`} className="action-link btn-primary">
            Quick connect
          </a>
        }
      />
      <div className={styles.content}>
        <div className={styles.headerRow}>
          <div className={styles.titleArea}>
            <div className={styles.healthDot} data-healthy={healthy ? "" : undefined} />
            <span className={styles.title}>Provide Terminal</span>
            {!loading && (
              <span style={{
                fontSize: 12, padding: "3px 10px", borderRadius: "var(--radius-pill)",
                background: healthy ? "var(--bg-success)" : "var(--bg-danger)",
                color: healthy ? "var(--text-success)" : "var(--text-danger)",
              }}>
                {healthy ? "All systems healthy" : `${errors.length} error(s)`}
              </span>
            )}
          </div>
          <button type="button" onClick={() => void refresh()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>

        {error && <div className={styles.error}>{error}</div>}

        <div className={styles.metricGrid}>
          <MetricCard label="Sessions" value={sessions.length} />
          <MetricCard label="Live" value={live.length} color="var(--success)" />
          <MetricCard label="Errors" value={errors.length} color="var(--danger)" />
          <MetricCard label="Recording" value={recording.length} />
        </div>

        <div className={styles.listHeader}>
          <span style={{ fontSize: 14, fontWeight: 500 }}>Sessions</span>
          <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>sorted by status</span>
          <div className="flex-spacer" />
          <FilterInput value={filter} onChange={setFilter} placeholder="Filter sessions..." />
        </div>

        <SessionList bootstrap={bootstrap} filter={filter} />
      </div>
    </PageShell>
  );
}
