//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionSummary } from "../api/types";
import { useDashboardStore } from "./dashboardStore";

const MOCK_SESSIONS: SessionSummary[] = [
  {
    sessionId: "s1",
    displayName: "Session One",
    connectorType: "pty",
    lifecycleState: "running",
    inputMode: "open",
    connected: true,
    autoStart: false,
    tags: [],
    recordingEnabled: false,
    recordingAvailable: false,
    owner: null,
    visibility: "public",
    lastError: null,
  },
  {
    sessionId: "s2",
    displayName: "Session Two",
    connectorType: "ssh",
    lifecycleState: "stopped",
    inputMode: "hijack",
    connected: false,
    autoStart: true,
    tags: ["prod"],
    recordingEnabled: true,
    recordingAvailable: true,
    owner: "admin",
    visibility: "private",
    lastError: null,
  },
];

vi.mock("../api/sessions", () => ({
  fetchSessions: vi.fn(),
  restartSession: vi.fn(),
}));

async function importMocks() {
  const mod = await import("../api/sessions");
  return {
    fetchSessions: vi.mocked(mod.fetchSessions),
    restartSession: vi.mocked(mod.restartSession),
  };
}

function resetStore() {
  useDashboardStore.setState({
    sessions: [],
    filter: "",
    loading: false,
    error: null,
  });
}

beforeEach(() => {
  resetStore();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetStore();
});

describe("dashboardStore", () => {
  describe("initial state", () => {
    it("has empty sessions", () => {
      expect(useDashboardStore.getState().sessions).toEqual([]);
    });

    it("has empty filter", () => {
      expect(useDashboardStore.getState().filter).toBe("");
    });

    it("is not loading", () => {
      expect(useDashboardStore.getState().loading).toBe(false);
    });

    it("has no error", () => {
      expect(useDashboardStore.getState().error).toBeNull();
    });
  });

  describe("setFilter", () => {
    it("updates the filter string", () => {
      useDashboardStore.getState().setFilter("ssh");
      expect(useDashboardStore.getState().filter).toBe("ssh");
    });

    it("can clear the filter", () => {
      useDashboardStore.getState().setFilter("ssh");
      useDashboardStore.getState().setFilter("");
      expect(useDashboardStore.getState().filter).toBe("");
    });
  });

  describe("refresh", () => {
    it("loads sessions on success", async () => {
      const mocks = await importMocks();
      mocks.fetchSessions.mockResolvedValue(MOCK_SESSIONS);

      const promise = useDashboardStore.getState().refresh();
      expect(useDashboardStore.getState().loading).toBe(true);

      await promise;
      const state = useDashboardStore.getState();
      expect(state.loading).toBe(false);
      expect(state.sessions).toEqual(MOCK_SESSIONS);
      expect(state.error).toBeNull();
    });

    it("sets error on fetch failure", async () => {
      const mocks = await importMocks();
      mocks.fetchSessions.mockRejectedValue(new Error("Server down"));

      await useDashboardStore.getState().refresh();
      const state = useDashboardStore.getState();
      expect(state.loading).toBe(false);
      expect(state.error).toContain("Server down");
    });

    it("clears previous error on new refresh", async () => {
      const mocks = await importMocks();
      mocks.fetchSessions.mockRejectedValueOnce(new Error("fail"));
      await useDashboardStore.getState().refresh();
      expect(useDashboardStore.getState().error).not.toBeNull();

      mocks.fetchSessions.mockResolvedValue(MOCK_SESSIONS);
      await useDashboardStore.getState().refresh();
      expect(useDashboardStore.getState().error).toBeNull();
    });
  });

  describe("restart", () => {
    it("restarts session and refreshes list", async () => {
      const mocks = await importMocks();
      mocks.restartSession.mockResolvedValue(MOCK_SESSIONS[0]!);
      mocks.fetchSessions.mockResolvedValue(MOCK_SESSIONS);

      await useDashboardStore.getState().restart("s1");
      expect(mocks.restartSession).toHaveBeenCalledWith("s1");
      expect(mocks.fetchSessions).toHaveBeenCalled();
      expect(useDashboardStore.getState().sessions).toEqual(MOCK_SESSIONS);
    });

    it("sets error on restart failure", async () => {
      const mocks = await importMocks();
      mocks.restartSession.mockRejectedValue(new Error("forbidden"));

      await useDashboardStore.getState().restart("s1");
      const state = useDashboardStore.getState();
      expect(state.error).toContain("Restart failed");
      expect(state.error).toContain("forbidden");
    });
  });
});
