//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect } from "react";
import type { AppBootstrap } from "../../api/types";
import { useSessionStore } from "../../stores/sessionStore";
import { useTerminalStore } from "../../stores/terminalStore";
import { StatusBadge } from "../common/StatusBadge";
import { AppHeader } from "../layout/AppHeader";
import { PageShell } from "../layout/PageShell";
import { HijackHost } from "../widgets/HijackHost";
import styles from "./SessionPage.module.css";

import type { SessionSummary } from "../../api/types";

interface SessionPageProps {
  bootstrap: AppBootstrap;
}

function SessionMeta({ summary }: { summary: SessionSummary | null }) {
  if (!summary) return null;
  return (
    <div className={styles.meta}>
      Mode: {summary.inputMode} · {summary.connectorType} · {summary.lifecycleState}
    </div>
  );
}

function SessionStatusBar({ summary }: { summary: SessionSummary | null }) {
  const mounted = useTerminalStore((s) => s.mounted);
  const cols = useTerminalStore((s) => s.cols);
  const rows = useTerminalStore((s) => s.rows);
  return (
    <div className={styles.statusBar}>
      <div className="status-dot" style={{
        background: mounted ? "var(--success)" : "var(--text-tertiary)",
        boxShadow: mounted ? "0 0 4px var(--success)" : undefined,
      }} />
      <span>{mounted ? "Connected" : "Disconnected"}</span>
      {cols > 0 && rows > 0 && (
        <>
          <span>·</span>
          <span style={{ fontFamily: "var(--font-mono)" }}>{cols}×{rows}</span>
        </>
      )}
      {summary && (
        <>
          <span>·</span>
          <span>{summary.inputMode === "open" ? "Shared" : "Exclusive"}</span>
        </>
      )}
    </div>
  );
}

export function SessionPage({ bootstrap }: SessionPageProps) {
  const sessionId = bootstrap.session_id;
  if (!sessionId) throw new Error("session bootstrap missing session_id");

  const { summary, loading, error, load } = useSessionStore();
  const termError = useTerminalStore((s) => s.error);

  useEffect(() => {
    void load(sessionId);
  }, [sessionId, load]);

  return (
    <PageShell>
      <AppHeader
        bootstrap={bootstrap}
        crumbs={[{ label: summary?.displayName ?? sessionId }]}
        right={summary && (
          <StatusBadge tone={summary.connected ? "ok" : summary.lastError ? "error" : "neutral"}>
            {summary.connected ? "Live" : summary.lastError ? "Error" : "Stopped"}
          </StatusBadge>
        )}
      />

      {loading && <div className={styles.loading}>Loading session…</div>}
      {error && <div className={styles.error}>{error}</div>}
      {termError && <div className={styles.error}>{termError}</div>}

      <SessionMeta summary={summary} />

      <div className={styles.terminal}>
        <HijackHost sessionId={sessionId} surface="user" />
      </div>

      <SessionStatusBar summary={summary} />
    </PageShell>
  );
}
