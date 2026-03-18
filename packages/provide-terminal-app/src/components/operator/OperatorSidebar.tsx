//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { restartSession } from "../../api/sessions";
import type { AppBootstrap } from "../../api/types";
import { useSessionStore } from "../../stores/sessionStore";
import { ModeToggle } from "./ModeToggle";
import { SessionMeta } from "./SessionMeta";

interface OperatorSidebarProps {
  sessionId: string;
  bootstrap: AppBootstrap;
}

export function OperatorSidebar({ sessionId, bootstrap }: OperatorSidebarProps) {
  const { summary, analysis, modePending, utilityPending, switchMode, clear, analyze } = useSessionStore();

  return (
    <>
      <div>
        <div className="section-label">Input mode</div>
        <ModeToggle
          mode={summary?.inputMode ?? "hijack"}
          disabled={modePending}
          onChange={(mode) => void switchMode(sessionId, mode)}
        />
      </div>

      <div>
        <div className="section-label">Actions</div>
        <div className="sidebar-actions">
          <button type="button" className="sidebar-btn"
            disabled={utilityPending} onClick={() => void analyze(sessionId)}>
            Analyze screen
          </button>
          <a className="action-link sidebar-btn"
            href={`${bootstrap.app_path}/replay/${encodeURIComponent(sessionId)}`}>
            View replay
          </a>
          <button type="button" className="sidebar-btn"
            disabled={utilityPending} onClick={() => void clear(sessionId)}>
            Clear runtime
          </button>
        </div>
      </div>

      {analysis && (
        <div>
          <div className="section-label">Analysis</div>
          <div className="code-box code-box-sm">
            {analysis}
          </div>
        </div>
      )}

      {summary && <SessionMeta summary={summary} />}

      <div className="flex-spacer" />

      <div className="border-section border-section-sm">
        <button type="button" className="btn-full btn-danger-outline" onClick={() => {
          void restartSession(sessionId).then(() => window.location.reload());
        }}>
          Restart session
        </button>
      </div>
    </>
  );
}
