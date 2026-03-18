//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { create } from "zustand";
import { fetchSessions, restartSession } from "../api/sessions";
import type { SessionSummary } from "../api/types";

interface DashboardState {
  sessions: SessionSummary[];
  filter: string;
  loading: boolean;
  error: string | null;

  setFilter: (filter: string) => void;
  refresh: () => Promise<void>;
  restart: (sessionId: string) => Promise<void>;
}

export const useDashboardStore = create<DashboardState>((set, get) => ({
  sessions: [],
  filter: "",
  loading: false,
  error: null,

  setFilter: (filter) => set({ filter }),

  refresh: async () => {
    set({ loading: true, error: null });
    try {
      const sessions = await fetchSessions();
      set({ sessions, loading: false });
    } catch (err) {
      set({ error: String(err), loading: false });
    }
  },

  restart: async (sessionId) => {
    try {
      await restartSession(sessionId);
      await get().refresh();
    } catch (err) {
      set({ error: `Restart failed: ${String(err)}` });
    }
  },
}));
