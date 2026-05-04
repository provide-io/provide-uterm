//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { create } from "zustand";
import type {
  HttpExchangeEntry,
  HttpRequestEntry,
  HttpResponseEntry,
} from "../api/types";

type WsStatus = "connecting" | "connected" | "disconnected";

export interface InspectState {
  exchanges: HttpExchangeEntry[];
  selected: string | null;
  methodFilter: string;
  urlFilter: string;
  inspectEnabled: boolean;
  interceptEnabled: boolean;
  interceptTimeout: number;
  interceptTimeoutAction: string;
  wsStatus: WsStatus;

  addRequest: (req: HttpRequestEntry) => void;
  addResponse: (res: HttpResponseEntry) => void;
  resolveIntercept: (id: string, action: string) => void;
  syncInterceptState: (state: {
    inspect_enabled: boolean;
    enabled: boolean;
    timeout_s: number;
    timeout_action: string;
  }) => void;
  select: (id: string | null) => void;
  setMethodFilter: (method: string) => void;
  setUrlFilter: (url: string) => void;
  setInspectEnabled: (enabled: boolean) => void;
  setInterceptEnabled: (enabled: boolean) => void;
  setWsStatus: (status: WsStatus) => void;
  clear: () => void;
}

export const useInspectStore = create<InspectState>((set) => ({
  exchanges: [],
  selected: null,
  methodFilter: "",
  urlFilter: "",
  inspectEnabled: true,
  interceptEnabled: false,
  interceptTimeout: 30,
  interceptTimeoutAction: "forward",
  wsStatus: "connecting",

  addRequest: (req) =>
    set((s) => ({
      exchanges: [
        ...s.exchanges,
        {
          id: req.id,
          request: req,
          response: null,
          intercepted: req.intercepted ?? false,
          interceptResolved: false,
          interceptAction: null,
        },
      ],
    })),

  addResponse: (res) =>
    set((s) => ({
      exchanges: s.exchanges.map((ex) =>
        ex.id === res.id ? { ...ex, response: res } : ex,
      ),
    })),

  resolveIntercept: (id, action) =>
    set((s) => ({
      exchanges: s.exchanges.map((ex) =>
        ex.id === id ? { ...ex, interceptResolved: true, interceptAction: action } : ex,
      ),
    })),

  syncInterceptState: (state) =>
    set({
      inspectEnabled: state.inspect_enabled !== false,
      interceptEnabled: Boolean(state.enabled),
      interceptTimeout: Number(state.timeout_s ?? 30),
      interceptTimeoutAction: String(state.timeout_action ?? "forward"),
    }),

  select: (id) => set({ selected: id }),
  setMethodFilter: (method) => set({ methodFilter: method }),
  setUrlFilter: (url) => set({ urlFilter: url }),
  setInspectEnabled: (enabled) => set({ inspectEnabled: enabled }),
  setInterceptEnabled: (enabled) => set({ interceptEnabled: enabled }),
  setWsStatus: (status) => set({ wsStatus: status }),
  clear: () =>
    set({
      exchanges: [],
      selected: null,
      methodFilter: "",
      urlFilter: "",
      inspectEnabled: true,
      interceptEnabled: false,
      interceptTimeout: 30,
      interceptTimeoutAction: "forward",
      wsStatus: "connecting",
    }),
}));
