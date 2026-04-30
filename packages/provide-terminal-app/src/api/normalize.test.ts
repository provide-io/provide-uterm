//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import { normalizeRecordingEntries, normalizeSessionStatus } from "./normalize";

describe("normalizeSessionStatus", () => {
  const RAW = {
    session_id: "abc",
    display_name: "My Session",
    connector_type: "pty",
    lifecycle_state: "running",
    input_mode: "open",
    connected: true,
    auto_start: false,
    tags: ["dev", "test"],
    recording_enabled: true,
    recording_available: false,
    owner: "admin",
    visibility: "private",
    last_error: "something broke",
  };

  it("transforms snake_case to camelCase", () => {
    const result = normalizeSessionStatus(RAW);
    expect(result.sessionId).toBe("abc");
    expect(result.displayName).toBe("My Session");
    expect(result.connectorType).toBe("pty");
    expect(result.lifecycleState).toBe("running");
    expect(result.connected).toBe(true);
    expect(result.autoStart).toBe(false);
    expect(result.recordingEnabled).toBe(true);
    expect(result.recordingAvailable).toBe(false);
    expect(result.owner).toBe("admin");
    expect(result.visibility).toBe("private");
    expect(result.lastError).toBe("something broke");
  });

  it("normalizes input_mode 'hijack' to 'hijack'", () => {
    const result = normalizeSessionStatus({ ...RAW, input_mode: "hijack" });
    expect(result.inputMode).toBe("hijack");
  });

  it("normalizes any non-hijack mode to 'open'", () => {
    const result = normalizeSessionStatus({ ...RAW, input_mode: "whatever" });
    expect(result.inputMode).toBe("open");
  });

  it("copies tags as a new array", () => {
    const original = { ...RAW, tags: ["a", "b"] };
    const result = normalizeSessionStatus(original);
    expect(result.tags).toEqual(["a", "b"]);
    // Must be a copy, not the same reference
    expect(result.tags).not.toBe(original.tags);
  });

  it("handles null owner", () => {
    const result = normalizeSessionStatus({ ...RAW, owner: null });
    expect(result.owner).toBeNull();
  });

  it("handles null last_error", () => {
    const result = normalizeSessionStatus({ ...RAW, last_error: null });
    expect(result.lastError).toBeNull();
  });

  it("defaults visibility to 'public' when undefined", () => {
    const rawWithoutVisibility = { ...RAW } as Record<string, unknown>;
    delete rawWithoutVisibility.visibility;
    // biome-ignore lint/suspicious/noExplicitAny: test edge case
    const result = normalizeSessionStatus(rawWithoutVisibility as any);
    expect(result.visibility).toBe("public");
  });
});

describe("normalizeRecordingEntries", () => {
  it("maps entries with all fields present", () => {
    const raw = [
      { ts: 100.5, event: "output", data: { screen: "hello", extra: 42 } },
    ];
    const result = normalizeRecordingEntries(raw);
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({
      ts: 100.5,
      event: "output",
      payload: { screen: "hello", extra: 42 },
      screen: "hello",
    });
  });

  it("handles missing ts as null", () => {
    const raw = [{ event: "input", data: {} }];
    const result = normalizeRecordingEntries(raw);
    expect(result[0]?.ts).toBeNull();
  });

  it("handles missing event as 'unknown'", () => {
    const raw = [{ ts: 1, data: {} }];
    const result = normalizeRecordingEntries(raw);
    expect(result[0]?.event).toBe("unknown");
  });

  it("handles missing data as empty payload", () => {
    const raw = [{ ts: 1, event: "resize" }];
    const result = normalizeRecordingEntries(raw);
    expect(result[0]?.payload).toEqual({});
    expect(result[0]?.screen).toBe("");
  });

  it("handles non-string screen in data", () => {
    const raw = [{ ts: 1, event: "output", data: { screen: 42 } }];
    const result = normalizeRecordingEntries(raw);
    expect(result[0]?.screen).toBe("");
  });

  it("processes multiple entries", () => {
    const raw = [
      { ts: 1, event: "a", data: { screen: "x" } },
      { ts: 2, event: "b", data: { screen: "y" } },
      { ts: 3, event: "c", data: {} },
    ];
    const result = normalizeRecordingEntries(raw);
    expect(result).toHaveLength(3);
    expect(result[0]?.screen).toBe("x");
    expect(result[1]?.screen).toBe("y");
    expect(result[2]?.screen).toBe("");
  });

  it("returns empty array for empty input", () => {
    expect(normalizeRecordingEntries([])).toEqual([]);
  });

  it("handles non-number ts as null", () => {
    // biome-ignore lint/suspicious/noExplicitAny: test edge case
    const raw = [{ ts: "not a number" as any, event: "x", data: {} }];
    const result = normalizeRecordingEntries(raw);
    expect(result[0]?.ts).toBeNull();
  });

  it("handles non-string event as 'unknown'", () => {
    // biome-ignore lint/suspicious/noExplicitAny: test edge case
    const raw = [{ ts: 1, event: 42 as any, data: {} }];
    const result = normalizeRecordingEntries(raw);
    expect(result[0]?.event).toBe("unknown");
  });
});
