//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect } from "react";
import type { AppBootstrap } from "../../api/types";
import { useReplayStore } from "../../stores/replayStore";
import { AppHeader } from "../layout/AppHeader";
import { PageShell } from "../layout/PageShell";
import { EventDetail } from "./EventDetail";
import { PlaybackControls } from "./PlaybackControls";
import { ScreenPreview } from "./ScreenPreview";
import { TimelineCanvas } from "./TimelineCanvas";
import styles from "./ReplayPage.module.css";

interface ReplayPageProps {
  bootstrap: AppBootstrap;
}

export function ReplayPage({ bootstrap }: ReplayPageProps) {
  const sessionId = bootstrap.session_id;
  if (!sessionId) throw new Error("replay bootstrap missing session_id");

  const { entries, index, loading, error, load } = useReplayStore();
  const entry = entries[index] ?? null;

  useEffect(() => {
    void load(sessionId);
  }, [sessionId, load]);

  return (
    <PageShell>
      <AppHeader
        bootstrap={bootstrap}
        crumbs={[
          { label: bootstrap.title, href: `${bootstrap.app_path}/operator/${encodeURIComponent(sessionId)}` },
          { label: "Replay" },
        ]}
        right={
          <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>
            {entries.length} events
          </span>
        }
      />

      {error && <div className={styles.error}>{error}</div>}

      <div className={styles.controls}>
        <PlaybackControls sessionId={sessionId} />
        <TimelineCanvas />
        <div className={styles.legend}>
          <LegendItem color="var(--success)" label="read" />
          <LegendItem color="var(--info)" label="send" />
          <LegendItem color="var(--danger)" label="error" />
          <LegendItem color="var(--warning)" label="runtime" />
        </div>
      </div>

      <div className={styles.splitPane}>
        <div className={styles.screenPane}>
          {loading ? (
            <div className={styles.loadingText}>Loading recording…</div>
          ) : (
            <ScreenPreview entry={entry} index={index} />
          )}
        </div>
        <div className={styles.detailPane}>
          <EventDetail entry={entry} />
        </div>
      </div>
    </PageShell>
  );
}

function LegendItem({ color, label }: { color: string; label: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <div style={{ width: 8, height: 8, borderRadius: 2, background: color }} />
      {label}
    </div>
  );
}
