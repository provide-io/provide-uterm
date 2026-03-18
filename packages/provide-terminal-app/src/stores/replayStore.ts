//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { create } from "zustand";
import { fetchRecordingEntries } from "../api/sessions";
import type { RecordingEntryView } from "../api/types";

type PlaybackSpeed = 0.5 | 1 | 2 | 4;

interface ReplayState {
  entries: RecordingEntryView[];
  index: number;
  filter: string;
  limit: number;
  loading: boolean;
  error: string | null;
  playing: boolean;
  speed: PlaybackSpeed;

  load: (sessionId: string) => Promise<void>;
  setFilter: (filter: string) => void;
  setLimit: (limit: number) => void;
  setIndex: (index: number) => void;
  prev: () => void;
  next: () => void;
  first: () => void;
  last: () => void;
  setPlaying: (playing: boolean) => void;
  setSpeed: (speed: PlaybackSpeed) => void;
}

let playbackTimer: ReturnType<typeof setTimeout> | null = null;

function stopPlayback() {
  if (playbackTimer !== null) {
    clearTimeout(playbackTimer);
    playbackTimer = null;
  }
}

function scheduleNext(get: () => ReplayState, set: (s: Partial<ReplayState>) => void) {
  stopPlayback();
  const { entries, index, playing, speed } = get();
  if (!playing || index >= entries.length - 1) {
    if (playing) set({ playing: false });
    return;
  }

  const current = entries[index];
  const next = entries[index + 1];
  let delayMs: number;

  if (current?.ts != null && next?.ts != null && next.ts > current.ts) {
    // Use real-time delta, clamped to 2s max (avoid huge gaps)
    delayMs = Math.min((next.ts - current.ts) * 1000, 2000) / speed;
  } else {
    // No timestamps or same timestamp — fixed interval
    delayMs = 200 / speed;
  }

  // Minimum 30ms to keep UI responsive
  delayMs = Math.max(delayMs, 30);

  playbackTimer = setTimeout(() => {
    const state = get();
    if (!state.playing) return;
    if (state.index < state.entries.length - 1) {
      set({ index: state.index + 1 });
      scheduleNext(get, set);
    } else {
      set({ playing: false });
    }
  }, delayMs);
}

export const useReplayStore = create<ReplayState>((set, get) => ({
  entries: [],
  index: 0,
  filter: "",
  limit: 200,
  loading: false,
  error: null,
  playing: false,
  speed: 1,

  load: async (sessionId) => {
    const { filter, limit } = get();
    stopPlayback();
    set({ loading: true, error: null, playing: false });
    try {
      const entries = await fetchRecordingEntries(sessionId, filter, limit);
      set({
        entries,
        index: entries.length > 0 ? entries.length - 1 : 0,
        loading: false,
      });
    } catch (err) {
      set({ error: String(err), loading: false });
    }
  },

  setFilter: (filter) => set({ filter }),
  setLimit: (limit) => set({ limit }),

  setIndex: (index) => {
    const { entries } = get();
    set({ index: Math.max(0, Math.min(index, entries.length - 1)) });
  },

  prev: () => {
    const { index } = get();
    if (index > 0) set({ index: index - 1 });
  },
  next: () => {
    const { index, entries } = get();
    if (index < entries.length - 1) set({ index: index + 1 });
  },
  first: () => set({ index: 0 }),
  last: () => {
    const { entries } = get();
    set({ index: Math.max(0, entries.length - 1) });
  },

  setPlaying: (playing) => {
    set({ playing });
    if (playing) {
      scheduleNext(get, set);
    } else {
      stopPlayback();
    }
  },
  setSpeed: (speed) => {
    set({ speed });
    // If currently playing, restart the timer with new speed
    if (get().playing) {
      scheduleNext(get, set);
    }
  },
}));
