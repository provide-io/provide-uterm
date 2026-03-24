//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SessionSummary } from "./types.js";

// We need to mock api.js before importing state.js
vi.mock("./api.js", () => ({
  fetchSessions: vi.fn(),
  fetchSessionDetails: vi.fn(),
  setSessionMode: vi.fn(),
  clearSession: vi.fn(),
  analyzeSession: vi.fn(),
  fetchRecordingEntries: vi.fn(),
}));

import * as apiModule from "./api.js";
import {
  clearRuntime,
  loadDashboardState,
  loadOperatorWorkspaceState,
  loadReplayState,
  loadSessionRuntimeState,
  loadUserWorkspaceState,
  requestAnalysis,
  sessionStatus,
  summarizeSessions,
  switchSessionMode,
} from "./state.js";

function makeSummary(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    sessionId: "sess-1",
    displayName: "Test",
    connectorType: "shell",
    lifecycleState: "running",
    inputMode: "hijack",
    connected: true,
    autoStart: false,
    tags: [],
    recordingEnabled: false,
    recordingAvailable: false,
    owner: null,
    visibility: "public",
    lastError: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("summarizeSessions", () => {
  it("categorizes running sessions", () => {
    const running = makeSummary({ connected: true, lifecycleState: "running" });
    const result = summarizeSessions([running]);
    expect(result.running).toHaveLength(1);
    expect(result.stopped).toHaveLength(0);
    expect(result.degraded).toHaveLength(0);
  });

  it("categorizes stopped sessions", () => {
    const stopped = makeSummary({ connected: false, lifecycleState: "stopped" });
    const result = summarizeSessions([stopped]);
    expect(result.running).toHaveLength(0);
    expect(result.stopped).toHaveLength(1);
    expect(result.degraded).toHaveLength(0);
  });

  it("categorizes error sessions by lifecycleState", () => {
    const degraded = makeSummary({ lifecycleState: "error", connected: false });
    const result = summarizeSessions([degraded]);
    expect(result.degraded).toHaveLength(1);
  });

  it("categorizes error sessions by lastError", () => {
    const degraded = makeSummary({ lastError: "some error", connected: false, lifecycleState: "stopped" });
    const result = summarizeSessions([degraded]);
    expect(result.degraded).toHaveLength(1);
  });

  it("handles empty list", () => {
    const result = summarizeSessions([]);
    expect(result.running).toHaveLength(0);
    expect(result.stopped).toHaveLength(0);
    expect(result.degraded).toHaveLength(0);
  });

  it("can categorize multiple sessions", () => {
    const sessions = [
      makeSummary({ connected: true, lifecycleState: "running", sessionId: "s1" }),
      makeSummary({ connected: false, lifecycleState: "stopped", sessionId: "s2" }),
      makeSummary({ lifecycleState: "error", connected: false, sessionId: "s3" }),
    ];
    const result = summarizeSessions(sessions);
    expect(result.running).toHaveLength(1);
    expect(result.stopped).toHaveLength(1);
    expect(result.degraded).toHaveLength(1);
  });
});

describe("sessionStatus", () => {
  it("returns error tone when lastError is set", () => {
    const summary = makeSummary({ lastError: "something broke" });
    const result = sessionStatus(summary);
    expect(result.tone).toBe("error");
    expect(result.text).toBe("something broke");
  });

  it("returns ok tone when connected", () => {
    const summary = makeSummary({ connected: true, lastError: null, inputMode: "open" });
    const result = sessionStatus(summary);
    expect(result.tone).toBe("ok");
    expect(result.text).toContain("live");
    expect(result.text).toContain("open mode");
  });

  it("returns info tone when offline", () => {
    const summary = makeSummary({ connected: false, lastError: null, displayName: "MySession" });
    const result = sessionStatus(summary);
    expect(result.tone).toBe("info");
    expect(result.text).toContain("MySession");
    expect(result.text).toContain("offline");
  });

  it("includes displayName and inputMode in ok status", () => {
    const summary = makeSummary({ displayName: "Demo", inputMode: "hijack", connected: true });
    const result = sessionStatus(summary);
    expect(result.text).toContain("Demo");
    expect(result.text).toContain("hijack");
  });
});

describe("loadDashboardState", () => {
  it("delegates to fetchSessions", async () => {
    const sessions = [makeSummary()];
    vi.mocked(apiModule.fetchSessions).mockResolvedValue(sessions);
    const result = await loadDashboardState();
    expect(result).toBe(sessions);
  });
});

describe("loadSessionRuntimeState", () => {
  it("returns runtime state with summary and snapshotPromptId", async () => {
    const summary = makeSummary();
    vi.mocked(apiModule.fetchSessionDetails).mockResolvedValue({
      summary,
      snapshotPromptId: "p1",
    });
    const result = await loadSessionRuntimeState("sess-1");
    expect(result.summary).toBe(summary);
    expect(result.snapshotPromptId).toBe("p1");
    expect(result.analysis).toBeNull();
  });
});

describe("loadOperatorWorkspaceState", () => {
  it("returns full workspace state", async () => {
    const summary = makeSummary({ connected: true });
    vi.mocked(apiModule.fetchSessionDetails).mockResolvedValue({
      summary,
      snapshotPromptId: null,
    });
    const result = await loadOperatorWorkspaceState("sess-1");
    expect(result.session.summary).toBe(summary);
    expect(result.status.tone).toBe("ok");
    expect(result.modeCommand.pending).toBe(false);
    expect(result.modeCommand.lastError).toBeNull();
    expect(result.utilityCommand.pending).toBe(false);
    expect(result.widget.mounted).toBe(false);
    expect(result.widget.error).toBeNull();
  });
});

describe("loadUserWorkspaceState", () => {
  it("returns user workspace state", async () => {
    const summary = makeSummary({ connected: false, lastError: null });
    vi.mocked(apiModule.fetchSessionDetails).mockResolvedValue({
      summary,
      snapshotPromptId: null,
    });
    const result = await loadUserWorkspaceState("sess-1");
    expect(result.session.summary).toBe(summary);
    expect(result.status.tone).toBe("info");
    expect(result.widget.mounted).toBe(false);
  });
});

describe("switchSessionMode", () => {
  it("calls setSessionMode and fetchSessionDetails", async () => {
    const summary = makeSummary({ inputMode: "open" });
    vi.mocked(apiModule.setSessionMode).mockResolvedValue(summary);
    vi.mocked(apiModule.fetchSessionDetails).mockResolvedValue({
      summary,
      snapshotPromptId: "px",
    });
    const result = await switchSessionMode("sess-1", "open");
    expect(result.summary).toBe(summary);
    expect(result.snapshotPromptId).toBe("px");
    expect(result.analysis).toBeNull();
    expect(apiModule.setSessionMode).toHaveBeenCalledWith("sess-1", "open");
  });
});

describe("clearRuntime", () => {
  it("calls clearSession and fetchSessionDetails", async () => {
    const summary = makeSummary();
    vi.mocked(apiModule.clearSession).mockResolvedValue(summary);
    vi.mocked(apiModule.fetchSessionDetails).mockResolvedValue({
      summary,
      snapshotPromptId: null,
    });
    const result = await clearRuntime("sess-1");
    expect(result.summary).toBe(summary);
    expect(apiModule.clearSession).toHaveBeenCalledWith("sess-1");
  });
});

describe("requestAnalysis", () => {
  it("delegates to analyzeSession", async () => {
    vi.mocked(apiModule.analyzeSession).mockResolvedValue("analysis result");
    const result = await requestAnalysis("sess-1");
    expect(result).toBe("analysis result");
  });
});

describe("loadReplayState", () => {
  it("returns replay state with entries", async () => {
    const entries = [{ ts: 1, event: "read", payload: {}, screen: "hello" }];
    vi.mocked(apiModule.fetchRecordingEntries).mockResolvedValue(entries);
    const result = await loadReplayState("sess-1", "", 200);
    expect(result.entries).toBe(entries);
    expect(result.index).toBe(0); // last index = entries.length - 1
    expect(result.filter).toBe("");
    expect(result.limit).toBe(200);
    expect(result.total).toBe(1);
    expect(result.status.tone).toBe("ok");
    expect(result.status.text).toContain("1 recording entries");
  });

  it("returns info status for empty entries", async () => {
    vi.mocked(apiModule.fetchRecordingEntries).mockResolvedValue([]);
    const result = await loadReplayState("sess-1", "read", 25);
    expect(result.entries).toHaveLength(0);
    expect(result.index).toBe(0);
    expect(result.status.tone).toBe("info");
    expect(result.status.text).toContain("No entries");
  });

  it("sets index to last entry when multiple entries", async () => {
    const entries = [
      { ts: 1, event: "a", payload: {}, screen: "" },
      { ts: 2, event: "b", payload: {}, screen: "" },
      { ts: 3, event: "c", payload: {}, screen: "" },
    ];
    vi.mocked(apiModule.fetchRecordingEntries).mockResolvedValue(entries);
    const result = await loadReplayState("sess-1", "", 200);
    expect(result.index).toBe(2); // entries.length - 1
  });
});
