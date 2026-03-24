//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SessionStatus } from "../server-common.js";
import {
  analyzeSession,
  clearSession,
  deleteSession,
  fetchRecordingEntries,
  fetchSessionDetails,
  fetchSessionSummary,
  fetchSessions,
  normalizeRecordingEntries,
  normalizeSessionStatus,
  quickConnect,
  restartSession,
  setSessionMode,
  widgetSurface,
} from "./api.js";

function makeSessionStatus(overrides: Partial<SessionStatus> = {}): SessionStatus {
  return {
    session_id: "sess-1",
    display_name: "Test Session",
    connector_type: "shell",
    lifecycle_state: "running",
    input_mode: "hijack",
    connected: true,
    auto_start: false,
    tags: ["a", "b"],
    recording_enabled: false,
    recording_available: false,
    owner: null,
    visibility: "public",
    last_error: null,
    ...overrides,
  };
}

function makeFetch(data: unknown, ok = true) {
  return vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 500,
    json: () => Promise.resolve(data),
  });
}

describe("normalizeSessionStatus", () => {
  it("maps all fields from raw status", () => {
    const raw = makeSessionStatus();
    const result = normalizeSessionStatus(raw);
    expect(result.sessionId).toBe("sess-1");
    expect(result.displayName).toBe("Test Session");
    expect(result.connectorType).toBe("shell");
    expect(result.lifecycleState).toBe("running");
    expect(result.inputMode).toBe("hijack");
    expect(result.connected).toBe(true);
    expect(result.autoStart).toBe(false);
    expect(result.tags).toEqual(["a", "b"]);
    expect(result.recordingEnabled).toBe(false);
    expect(result.recordingAvailable).toBe(false);
    expect(result.owner).toBeNull();
    expect(result.visibility).toBe("public");
    expect(result.lastError).toBeNull();
  });

  it("normalizes input_mode 'open' to 'open'", () => {
    const raw = makeSessionStatus({ input_mode: "open" });
    expect(normalizeSessionStatus(raw).inputMode).toBe("open");
  });

  it("normalizes unknown input_mode to 'open'", () => {
    const raw = makeSessionStatus({ input_mode: "unknown-mode" });
    expect(normalizeSessionStatus(raw).inputMode).toBe("open");
  });

  it("falls back visibility to 'public' when undefined", () => {
    const raw = makeSessionStatus({ visibility: undefined as unknown as string });
    expect(normalizeSessionStatus(raw).visibility).toBe("public");
  });

  it("handles non-null owner", () => {
    const raw = makeSessionStatus({ owner: "alice" });
    expect(normalizeSessionStatus(raw).owner).toBe("alice");
  });

  it("copies tags array (not same reference)", () => {
    const tags = ["x", "y"];
    const raw = makeSessionStatus({ tags });
    const result = normalizeSessionStatus(raw);
    expect(result.tags).toEqual(tags);
    expect(result.tags).not.toBe(tags);
  });
});

describe("normalizeRecordingEntries", () => {
  it("maps entries with all fields present", () => {
    const entries = [{ ts: 1000, event: "read", data: { screen: "hello" } }];
    const result = normalizeRecordingEntries(entries);
    expect(result).toHaveLength(1);
    expect(result[0].ts).toBe(1000);
    expect(result[0].event).toBe("read");
    expect(result[0].screen).toBe("hello");
    expect(result[0].payload).toEqual({ screen: "hello" });
  });

  it("handles missing ts (sets null)", () => {
    const entries = [{ event: "read", data: {} }];
    const result = normalizeRecordingEntries(entries);
    expect(result[0].ts).toBeNull();
  });

  it("handles missing event (sets 'unknown')", () => {
    const entries = [{ ts: 100, data: {} }];
    const result = normalizeRecordingEntries(entries);
    expect(result[0].event).toBe("unknown");
  });

  it("handles missing data (uses empty object)", () => {
    const entries = [{ ts: 100, event: "x" }];
    const result = normalizeRecordingEntries(entries);
    expect(result[0].payload).toEqual({});
    expect(result[0].screen).toBe("");
  });

  it("handles missing screen in data (sets empty string)", () => {
    const entries = [{ ts: 100, event: "x", data: { other: "val" } }];
    const result = normalizeRecordingEntries(entries);
    expect(result[0].screen).toBe("");
  });

  it("handles empty entries array", () => {
    expect(normalizeRecordingEntries([])).toEqual([]);
  });
});

describe("fetchSessions", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fetches and normalizes sessions list", async () => {
    vi.stubGlobal("fetch", makeFetch([makeSessionStatus()]));
    const result = await fetchSessions();
    expect(result).toHaveLength(1);
    expect(result[0].sessionId).toBe("sess-1");
  });
});

describe("fetchSessionSummary", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fetches single session and normalizes", async () => {
    vi.stubGlobal("fetch", makeFetch(makeSessionStatus({ session_id: "s42" })));
    const result = await fetchSessionSummary("s42");
    expect(result.sessionId).toBe("s42");
  });

  it("encodes sessionId in URL", async () => {
    const mockFetch = makeFetch(makeSessionStatus());
    vi.stubGlobal("fetch", mockFetch);
    await fetchSessionSummary("my session/id");
    expect(mockFetch.mock.calls[0][0]).toContain("my%20session%2Fid");
  });
});

describe("fetchSessionDetails", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns summary and snapshotPromptId when present", async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(makeSessionStatus()),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ prompt_detected: { prompt_id: "p1" } }),
      });
    vi.stubGlobal("fetch", mockFetch);
    const result = await fetchSessionDetails("sess-1");
    expect(result.summary.sessionId).toBe("sess-1");
    expect(result.snapshotPromptId).toBe("p1");
  });

  it("returns null snapshotPromptId when snapshot is null", async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(makeSessionStatus()),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(null),
      });
    vi.stubGlobal("fetch", mockFetch);
    const result = await fetchSessionDetails("sess-1");
    expect(result.snapshotPromptId).toBeNull();
  });

  it("returns null snapshotPromptId when prompt_detected is missing", async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(makeSessionStatus()),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({}),
      });
    vi.stubGlobal("fetch", mockFetch);
    const result = await fetchSessionDetails("sess-1");
    expect(result.snapshotPromptId).toBeNull();
  });
});

describe("setSessionMode", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts mode and returns normalized result", async () => {
    const mockFetch = makeFetch(makeSessionStatus({ input_mode: "open" }));
    vi.stubGlobal("fetch", mockFetch);
    const result = await setSessionMode("sess-1", "open");
    expect(result.inputMode).toBe("open");
    const callArgs = mockFetch.mock.calls[0][1] as RequestInit;
    expect(callArgs.method).toBe("POST");
    expect(callArgs.body).toContain('"input_mode":"open"');
  });
});

describe("clearSession", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts to clear endpoint", async () => {
    const mockFetch = makeFetch(makeSessionStatus());
    vi.stubGlobal("fetch", mockFetch);
    const result = await clearSession("sess-1");
    expect(result.sessionId).toBe("sess-1");
    expect(mockFetch.mock.calls[0][0]).toContain("/clear");
  });
});

describe("restartSession", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts to restart endpoint", async () => {
    const mockFetch = makeFetch(makeSessionStatus());
    vi.stubGlobal("fetch", mockFetch);
    const result = await restartSession("sess-1");
    expect(result.sessionId).toBe("sess-1");
    expect(mockFetch.mock.calls[0][0]).toContain("/restart");
  });
});

describe("deleteSession", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends DELETE request", async () => {
    const mockFetch = makeFetch({ ok: true });
    vi.stubGlobal("fetch", mockFetch);
    await deleteSession("sess-1");
    const callArgs = mockFetch.mock.calls[0][1] as RequestInit;
    expect(callArgs.method).toBe("DELETE");
  });
});

describe("analyzeSession", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns analysis string", async () => {
    const mockFetch = makeFetch({ session_id: "sess-1", analysis: "AI analysis text" });
    vi.stubGlobal("fetch", mockFetch);
    const result = await analyzeSession("sess-1");
    expect(result).toBe("AI analysis text");
  });
});

describe("fetchRecordingEntries", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fetches and normalizes recording entries", async () => {
    const mockFetch = makeFetch([{ ts: 1, event: "read", data: { screen: "s" } }]);
    vi.stubGlobal("fetch", mockFetch);
    const result = await fetchRecordingEntries("sess-1", "", 200);
    expect(result).toHaveLength(1);
    expect(result[0].event).toBe("read");
  });

  it("includes limit param in query string", async () => {
    const mockFetch = makeFetch([]);
    vi.stubGlobal("fetch", mockFetch);
    await fetchRecordingEntries("sess-1", "", 25);
    expect(mockFetch.mock.calls[0][0]).toContain("limit=25");
  });

  it("includes event filter when non-empty", async () => {
    const mockFetch = makeFetch([]);
    vi.stubGlobal("fetch", mockFetch);
    await fetchRecordingEntries("sess-1", "read", 100);
    expect(mockFetch.mock.calls[0][0]).toContain("event=read");
  });

  it("does not include event filter when empty", async () => {
    const mockFetch = makeFetch([]);
    vi.stubGlobal("fetch", mockFetch);
    await fetchRecordingEntries("sess-1", "", 100);
    expect(mockFetch.mock.calls[0][0]).not.toContain("event=");
  });
});

describe("quickConnect", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts to /api/connect and returns result", async () => {
    const mockFetch = makeFetch({ session_id: "new-sess", url: "/operator/new-sess" });
    vi.stubGlobal("fetch", mockFetch);
    const result = await quickConnect({ connector_type: "shell" });
    expect(result.session_id).toBe("new-sess");
    expect(result.url).toBe("/operator/new-sess");
    expect(mockFetch.mock.calls[0][0]).toBe("/api/connect");
  });
});

describe("widgetSurface", () => {
  it("returns showAnalysis=true for operator surface", () => {
    const result = widgetSurface("operator");
    expect(result.showAnalysis).toBe(true);
    expect(result.mobileKeys).toBe(true);
  });

  it("returns showAnalysis=false for user surface", () => {
    const result = widgetSurface("user");
    expect(result.showAnalysis).toBe(false);
    expect(result.mobileKeys).toBe(false);
  });

  it("returns showAnalysis=false for undefined surface", () => {
    const result = widgetSurface(undefined);
    expect(result.showAnalysis).toBe(false);
    expect(result.mobileKeys).toBe(false);
  });
});
