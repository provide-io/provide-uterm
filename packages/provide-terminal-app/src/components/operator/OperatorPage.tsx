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
import { OperatorSidebar } from "./OperatorSidebar";
import styles from "./OperatorPage.module.css";

interface OperatorPageProps {
  bootstrap: AppBootstrap;
}

export function OperatorPage({ bootstrap }: OperatorPageProps) {
  const sessionId = bootstrap.session_id;
  if (!sessionId) throw new Error("operator bootstrap missing session_id");

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
        right={
          <>
            {summary && (
              <StatusBadge tone={summary.connected ? "ok" : summary.lastError ? "error" : "neutral"}>
                {summary.connected ? "Live" : summary.lastError ? "Error" : "Stopped"}
              </StatusBadge>
            )}
            {summary?.recordingEnabled && <StatusBadge tone="warning">recording</StatusBadge>}
            {summary?.connected && (
              <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>connected</span>
            )}
          </>
        }
      />
      {loading && <div className={styles.loading}>Loading operator workspace…</div>}
      {error && <div className={styles.error}>{error}</div>}
      {termError && <div className={styles.error}>{termError}</div>}

      <div className={styles.layout}>
        <div className={styles.sidebar}>
          <OperatorSidebar sessionId={sessionId} bootstrap={bootstrap} />
        </div>
        <div className={styles.main}>
          <div className={styles.terminal}>
            <HijackHost sessionId={sessionId} surface="operator" />
          </div>
          <div className={styles.statusBar}>
            <TerminalStatusDot />
          </div>
        </div>
      </div>
    </PageShell>
  );
}

function TerminalStatusDot() {
  const mounted = useTerminalStore((s) => s.mounted);
  const cols = useTerminalStore((s) => s.cols);
  const rows = useTerminalStore((s) => s.rows);
  return (
    <>
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
    </>
  );
}
