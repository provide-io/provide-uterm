//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AppBootstrap } from "../../api/types";
import { AppHeader } from "../layout/AppHeader";
import { PageShell } from "../layout/PageShell";
import { ConnectForm } from "./ConnectForm";
import { PresetCard } from "./PresetCard";
import styles from "./ConnectPage.module.css";

interface ConnectPageProps {
  bootstrap: AppBootstrap;
}

interface RecentConnection {
  host: string;
  transport: string;
  port: number;
  lastUsed: string;
}

function loadRecents(): RecentConnection[] {
  try {
    const raw = localStorage.getItem("uterm-recent-connections");
    if (!raw) return [];
    return JSON.parse(raw) as RecentConnection[];
  } catch {
    return [];
  }
}

export function saveRecent(conn: RecentConnection) {
  const recents = loadRecents().filter((r) => !(r.host === conn.host && r.transport === conn.transport && r.port === conn.port));
  recents.unshift(conn);
  localStorage.setItem("uterm-recent-connections", JSON.stringify(recents.slice(0, 6)));
}

export function ConnectPage({ bootstrap }: ConnectPageProps) {
  const recents = loadRecents();

  return (
    <PageShell>
      <AppHeader bootstrap={bootstrap} crumbs={[{ label: "Quick connect" }]} />
      <div className={styles.content}>
        {recents.length > 0 && (
          <div className={styles.section}>
            <div className={styles.sectionLabel}>Recent connections</div>
            <div className={styles.presetGrid}>
              {recents.slice(0, 3).map((r) => (
                <PresetCard key={`${r.host}:${r.port}`} connection={r} />
              ))}
            </div>
          </div>
        )}

        <div className={styles.divider} />

        <div className={styles.section}>
          <div className={styles.sectionLabel}>New connection</div>
          <ConnectForm bootstrap={bootstrap} />
        </div>

        <div className={styles.divider} />

        <div className={styles.section}>
          <div className={styles.sectionLabel}>Keyboard shortcuts</div>
          <div className={styles.shortcutGrid}>
            <ShortcutRow label="Focus terminal" keys="Esc" />
            <ShortcutRow label="Toggle settings" keys="Ctrl+," />
            <ShortcutRow label="Switch mode" keys="Ctrl+M" />
            <ShortcutRow label="Command palette" keys="Ctrl+K" />
          </div>
        </div>
      </div>
    </PageShell>
  );
}

function ShortcutRow({ label, keys }: { label: string; keys: string }) {
  return (
    <div className={styles.shortcutRow}>
      <span>{label}</span>
      <kbd className={styles.kbd}>{keys}</kbd>
    </div>
  );
}
