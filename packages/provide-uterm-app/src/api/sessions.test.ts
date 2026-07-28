//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  apiJson: vi.fn(),
}));

async function importMocks() {
  const { apiJson } = await import("./client");
  return { apiJson: vi.mocked(apiJson) };
}

const RAW_SESSION = {
  session_id: "s1",
  display_name: "S1",
  connector_type: "pty",
  lifecycle_state: "running",
  input_mode: "open",
  connected: true,
  auto_start: false,
  tags: [],
  recording_enabled: false,
  recording_available: false,
  owner: null,
  visibility: "public",
  last_error: null,
};

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("fetchSessions", () => {
  it("fetches and normalizes sessions list", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue([RAW_SESSION]);
    const { fetchSessions } = await import("./sessions");
    const result = await fetchSessions();
    expect(result).toHaveLength(1);
    expect(result[0]?.sessionId).toBe("s1");
    expect(mocks.apiJson).toHaveBeenCalledWith("/api/sessions", "GET", null);
  });
});

describe("fetchSessionSummary", () => {
  it("fetches single session and normalizes", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue(RAW_SESSION);
    const { fetchSessionSummary } = await import("./sessions");
    const result = await fetchSessionSummary("s1");
    expect(result.sessionId).toBe("s1");
    expect(mocks.apiJson).toHaveBeenCalledWith("/api/sessions/s1", "GET", null);
  });

  it("refuses a session id no route could carry", async () => {
    // It used to percent-encode this and send it. The shared contract bounds
    // a session id to an ASCII alphabet with no slash, dot or space in it, so
    // the encoded form matched no route on either backend — the request could
    // only ever have 404ed. Refusing here reports the offending value instead.
    const mocks = await importMocks();
    const { fetchSessionSummary } = await import("./sessions");
    await expect(fetchSessionSummary("a/b c")).rejects.toThrow(/session_id/);
    expect(mocks.apiJson).not.toHaveBeenCalled();
  });
});

describe("fetchSessionDetails", () => {
  it("fetches summary and snapshot in parallel", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValueOnce(RAW_SESSION).mockResolvedValueOnce({ prompt_detected: { prompt_id: "p1" } });
    const { fetchSessionDetails } = await import("./sessions");
    const result = await fetchSessionDetails("s1");
    expect(result.summary.sessionId).toBe("s1");
    expect(result.snapshotPromptId).toBe("p1");
  });

  it("returns null snapshotPromptId when snapshot is null", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValueOnce(RAW_SESSION).mockResolvedValueOnce(null);
    const { fetchSessionDetails } = await import("./sessions");
    const result = await fetchSessionDetails("s1");
    expect(result.snapshotPromptId).toBeNull();
  });

  it("returns null snapshotPromptId when prompt_detected is null", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValueOnce(RAW_SESSION).mockResolvedValueOnce({ prompt_detected: null });
    const { fetchSessionDetails } = await import("./sessions");
    const result = await fetchSessionDetails("s1");
    expect(result.snapshotPromptId).toBeNull();
  });
});

describe("setSessionMode", () => {
  it("sends POST with input_mode body", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue({ ...RAW_SESSION, input_mode: "hijack" });
    const { setSessionMode } = await import("./sessions");
    const result = await setSessionMode("s1", "hijack");
    expect(result.inputMode).toBe("hijack");
    expect(mocks.apiJson).toHaveBeenCalledWith("/api/sessions/s1/mode", "POST", { input_mode: "hijack" });
  });
});

describe("clearSession", () => {
  it("sends POST to clear endpoint", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue(RAW_SESSION);
    const { clearSession } = await import("./sessions");
    const result = await clearSession("s1");
    expect(result.sessionId).toBe("s1");
    expect(mocks.apiJson).toHaveBeenCalledWith("/api/sessions/s1/clear", "POST", null);
  });
});

describe("restartSession", () => {
  it("sends POST to restart endpoint", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue(RAW_SESSION);
    const { restartSession } = await import("./sessions");
    const result = await restartSession("s1");
    expect(result.sessionId).toBe("s1");
    expect(mocks.apiJson).toHaveBeenCalledWith("/api/sessions/s1/restart", "POST", null);
  });
});

describe("analyzeSession", () => {
  it("returns analysis string", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue({ analysis: "All clear" });
    const { analyzeSession } = await import("./sessions");
    const result = await analyzeSession("s1");
    expect(result).toBe("All clear");
  });
});

describe("fetchRecordingEntries", () => {
  it("passes limit and filter as query params", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue([]);
    const { fetchRecordingEntries } = await import("./sessions");
    await fetchRecordingEntries("s1", "output", 100);
    const url = mocks.apiJson.mock.calls[0]?.[0] as string;
    expect(url).toContain("limit=100");
    expect(url).toContain("event=output");
  });

  it("omits event param when filter is empty", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue([]);
    const { fetchRecordingEntries } = await import("./sessions");
    await fetchRecordingEntries("s1", "", 200);
    const url = mocks.apiJson.mock.calls[0]?.[0] as string;
    expect(url).toContain("limit=200");
    expect(url).not.toContain("event=");
  });

  it("normalizes raw entries", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue([{ ts: 1, event: "output", data: { screen: "hi" } }]);
    const { fetchRecordingEntries } = await import("./sessions");
    const result = await fetchRecordingEntries("s1", "", 200);
    expect(result).toHaveLength(1);
    expect(result[0]?.screen).toBe("hi");
  });
});

describe("quickConnect", () => {
  it("sends POST with payload and returns result", async () => {
    const mocks = await importMocks();
    mocks.apiJson.mockResolvedValue({ session_id: "new-s", url: "/s/new-s" });
    const { quickConnect } = await import("./sessions");
    const result = await quickConnect({
      connector_type: "ssh",
      host: "example.com",
      port: 22,
    });
    expect(result.session_id).toBe("new-s");
    expect(mocks.apiJson).toHaveBeenCalledWith("/api/connect", "POST", {
      connector_type: "ssh",
      host: "example.com",
      port: 22,
    });
  });
});
