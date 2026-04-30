//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionSummary } from "../api/types";
import { useSessionStore } from "./sessionStore";

const MOCK_SUMMARY: SessionSummary = {
  sessionId: "s1",
  displayName: "Test Session",
  connectorType: "pty",
  lifecycleState: "running",
  inputMode: "open",
  connected: true,
  autoStart: false,
  tags: ["dev"],
  recordingEnabled: true,
  recordingAvailable: true,
  owner: null,
  visibility: "public",
  lastError: null,
};

vi.mock("../api/sessions", () => ({
  fetchSessionDetails: vi.fn(),
  setSessionMode: vi.fn(),
  clearSession: vi.fn(),
  analyzeSession: vi.fn(),
}));

async function importMocks() {
  const mod = await import("../api/sessions");
  return {
    fetchSessionDetails: vi.mocked(mod.fetchSessionDetails),
    setSessionMode: vi.mocked(mod.setSessionMode),
    clearSession: vi.mocked(mod.clearSession),
    analyzeSession: vi.mocked(mod.analyzeSession),
  };
}

function resetStore() {
  useSessionStore.setState({
    summary: null,
    snapshotPromptId: null,
    analysis: null,
    loading: false,
    error: null,
    modePending: false,
    utilityPending: false,
  });
}

beforeEach(() => {
  resetStore();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetStore();
});

describe("sessionStore", () => {
  describe("initial state", () => {
    it("has null summary", () => {
      expect(useSessionStore.getState().summary).toBeNull();
    });

    it("is not loading", () => {
      expect(useSessionStore.getState().loading).toBe(false);
    });

    it("has no error", () => {
      expect(useSessionStore.getState().error).toBeNull();
    });
  });

  describe("load", () => {
    it("sets loading true then loads summary on success", async () => {
      const mocks = await importMocks();
      mocks.fetchSessionDetails.mockResolvedValue({
        summary: MOCK_SUMMARY,
        snapshotPromptId: "prompt-42",
      });

      const promise = useSessionStore.getState().load("s1");
      expect(useSessionStore.getState().loading).toBe(true);

      await promise;
      const state = useSessionStore.getState();
      expect(state.loading).toBe(false);
      expect(state.summary).toEqual(MOCK_SUMMARY);
      expect(state.snapshotPromptId).toBe("prompt-42");
    });

    it("sets error on fetch failure", async () => {
      const mocks = await importMocks();
      mocks.fetchSessionDetails.mockRejectedValue(new Error("Network error"));

      await useSessionStore.getState().load("s1");
      const state = useSessionStore.getState();
      expect(state.loading).toBe(false);
      expect(state.error).toContain("Network error");
      expect(state.summary).toBeNull();
    });
  });

  describe("switchMode", () => {
    it("sets modePending and updates summary on success", async () => {
      const mocks = await importMocks();
      const hijackSummary = { ...MOCK_SUMMARY, inputMode: "hijack" as const };
      mocks.setSessionMode.mockResolvedValue(hijackSummary);

      const promise = useSessionStore.getState().switchMode("s1", "hijack");
      expect(useSessionStore.getState().modePending).toBe(true);

      await promise;
      const state = useSessionStore.getState();
      expect(state.modePending).toBe(false);
      expect(state.summary?.inputMode).toBe("hijack");
    });

    it("sets error on failure", async () => {
      const mocks = await importMocks();
      mocks.setSessionMode.mockRejectedValue(new Error("forbidden"));

      await useSessionStore.getState().switchMode("s1", "hijack");
      const state = useSessionStore.getState();
      expect(state.modePending).toBe(false);
      expect(state.error).toContain("Mode switch failed");
      expect(state.error).toContain("forbidden");
    });
  });

  describe("clear", () => {
    it("clears session and resets analysis on success", async () => {
      const mocks = await importMocks();
      mocks.clearSession.mockResolvedValue(MOCK_SUMMARY);
      useSessionStore.setState({ analysis: "old analysis" });

      const promise = useSessionStore.getState().clear("s1");
      expect(useSessionStore.getState().utilityPending).toBe(true);

      await promise;
      const state = useSessionStore.getState();
      expect(state.utilityPending).toBe(false);
      expect(state.summary).toEqual(MOCK_SUMMARY);
      expect(state.analysis).toBeNull();
    });

    it("sets error on failure", async () => {
      const mocks = await importMocks();
      mocks.clearSession.mockRejectedValue(new Error("denied"));

      await useSessionStore.getState().clear("s1");
      const state = useSessionStore.getState();
      expect(state.utilityPending).toBe(false);
      expect(state.error).toContain("Clear failed");
      expect(state.error).toContain("denied");
    });
  });

  describe("analyze", () => {
    it("stores analysis text on success", async () => {
      const mocks = await importMocks();
      mocks.analyzeSession.mockResolvedValue("Session looks clean");

      const promise = useSessionStore.getState().analyze("s1");
      expect(useSessionStore.getState().utilityPending).toBe(true);

      await promise;
      const state = useSessionStore.getState();
      expect(state.utilityPending).toBe(false);
      expect(state.analysis).toBe("Session looks clean");
    });

    it("sets error on failure", async () => {
      const mocks = await importMocks();
      mocks.analyzeSession.mockRejectedValue(new Error("timeout"));

      await useSessionStore.getState().analyze("s1");
      const state = useSessionStore.getState();
      expect(state.utilityPending).toBe(false);
      expect(state.error).toContain("Analyze failed");
      expect(state.error).toContain("timeout");
    });
  });
});
