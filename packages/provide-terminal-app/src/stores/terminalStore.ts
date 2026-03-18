//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { create } from "zustand";

interface TerminalState {
  mounted: boolean;
  error: string | null;
  connectionStatus: "disconnected" | "connecting" | "connected";
  cols: number;
  rows: number;

  setMounted: (mounted: boolean, error?: string | null) => void;
  setConnectionStatus: (status: "disconnected" | "connecting" | "connected") => void;
  setDimensions: (cols: number, rows: number) => void;
}

export const useTerminalStore = create<TerminalState>((set) => ({
  mounted: false,
  error: null,
  connectionStatus: "disconnected",
  cols: 0,
  rows: 0,

  setMounted: (mounted, error = null) => set({ mounted, error }),
  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  setDimensions: (cols, rows) => set({ cols, rows }),
}));
