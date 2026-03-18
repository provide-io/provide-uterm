//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { create } from "zustand";
import { analyzeSession, clearSession, fetchSessionDetails, setSessionMode } from "../api/sessions";
import type { SessionMode, SessionSummary } from "../api/types";

interface SessionState {
  summary: SessionSummary | null;
  snapshotPromptId: string | null;
  analysis: string | null;
  loading: boolean;
  error: string | null;
  modePending: boolean;
  utilityPending: boolean;

  load: (sessionId: string) => Promise<void>;
  switchMode: (sessionId: string, mode: SessionMode) => Promise<void>;
  clear: (sessionId: string) => Promise<void>;
  analyze: (sessionId: string) => Promise<void>;
}

export const useSessionStore = create<SessionState>((set) => ({
  summary: null,
  snapshotPromptId: null,
  analysis: null,
  loading: false,
  error: null,
  modePending: false,
  utilityPending: false,

  load: async (sessionId) => {
    set({ loading: true, error: null });
    try {
      const details = await fetchSessionDetails(sessionId);
      set({
        summary: details.summary,
        snapshotPromptId: details.snapshotPromptId,
        loading: false,
      });
    } catch (err) {
      set({ error: String(err), loading: false });
    }
  },

  switchMode: async (sessionId, mode) => {
    set({ modePending: true, error: null });
    try {
      const summary = await setSessionMode(sessionId, mode);
      set({ summary, modePending: false });
    } catch (err) {
      set({ error: `Mode switch failed: ${String(err)}`, modePending: false });
    }
  },

  clear: async (sessionId) => {
    set({ utilityPending: true, error: null });
    try {
      const summary = await clearSession(sessionId);
      set({ summary, analysis: null, utilityPending: false });
    } catch (err) {
      set({ error: `Clear failed: ${String(err)}`, utilityPending: false });
    }
  },

  analyze: async (sessionId) => {
    set({ utilityPending: true, error: null });
    try {
      const analysis = await analyzeSession(sessionId);
      set({ analysis, utilityPending: false });
    } catch (err) {
      set({ error: `Analyze failed: ${String(err)}`, utilityPending: false });
    }
  },
}));
