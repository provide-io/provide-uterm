//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RecordingEntryView } from "../api/types";
import { useReplayStore } from "./replayStore";

const MOCK_ENTRIES: RecordingEntryView[] = [
  { ts: 1000.0, event: "output", payload: { screen: "hello" }, screen: "hello" },
  { ts: 1000.5, event: "output", payload: { screen: "world" }, screen: "world" },
  { ts: 1001.0, event: "input", payload: {}, screen: "" },
  { ts: 1002.0, event: "output", payload: { screen: "done" }, screen: "done" },
];

vi.mock("../api/sessions", () => ({
  fetchRecordingEntries: vi.fn(),
}));

async function importMocks() {
  const mod = await import("../api/sessions");
  return { fetchRecordingEntries: vi.mocked(mod.fetchRecordingEntries) };
}

function resetStore() {
  useReplayStore.setState({
    entries: [],
    index: 0,
    filter: "",
    limit: 200,
    loading: false,
    error: null,
    playing: false,
    speed: 1,
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  resetStore();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  resetStore();
});

describe("replayStore", () => {
  describe("initial state", () => {
    it("starts with empty entries", () => {
      expect(useReplayStore.getState().entries).toEqual([]);
    });

    it("starts at index 0", () => {
      expect(useReplayStore.getState().index).toBe(0);
    });

    it("is not playing", () => {
      expect(useReplayStore.getState().playing).toBe(false);
    });

    it("default speed is 1", () => {
      expect(useReplayStore.getState().speed).toBe(1);
    });

    it("default limit is 200", () => {
      expect(useReplayStore.getState().limit).toBe(200);
    });
  });

  describe("load", () => {
    it("loads entries and sets index to last entry", async () => {
      const mocks = await importMocks();
      mocks.fetchRecordingEntries.mockResolvedValue(MOCK_ENTRIES);

      await useReplayStore.getState().load("s1");
      const state = useReplayStore.getState();
      expect(state.entries).toEqual(MOCK_ENTRIES);
      expect(state.index).toBe(3); // last entry
      expect(state.loading).toBe(false);
    });

    it("passes filter and limit to API", async () => {
      const mocks = await importMocks();
      mocks.fetchRecordingEntries.mockResolvedValue([]);

      useReplayStore.setState({ filter: "output", limit: 50 });
      await useReplayStore.getState().load("s1");
      expect(mocks.fetchRecordingEntries).toHaveBeenCalledWith("s1", "output", 50);
    });

    it("sets index to 0 for empty entries", async () => {
      const mocks = await importMocks();
      mocks.fetchRecordingEntries.mockResolvedValue([]);

      await useReplayStore.getState().load("s1");
      expect(useReplayStore.getState().index).toBe(0);
    });

    it("stops playback when loading", async () => {
      const mocks = await importMocks();
      mocks.fetchRecordingEntries.mockResolvedValue(MOCK_ENTRIES);
      useReplayStore.setState({ playing: true });

      await useReplayStore.getState().load("s1");
      expect(useReplayStore.getState().playing).toBe(false);
    });

    it("sets error on failure", async () => {
      const mocks = await importMocks();
      mocks.fetchRecordingEntries.mockRejectedValue(new Error("fetch failed"));

      await useReplayStore.getState().load("s1");
      expect(useReplayStore.getState().error).toContain("fetch failed");
      expect(useReplayStore.getState().loading).toBe(false);
    });
  });

  describe("navigation", () => {
    beforeEach(() => {
      useReplayStore.setState({ entries: MOCK_ENTRIES, index: 1 });
    });

    it("prev decrements index", () => {
      useReplayStore.getState().prev();
      expect(useReplayStore.getState().index).toBe(0);
    });

    it("prev does not go below 0", () => {
      useReplayStore.setState({ index: 0 });
      useReplayStore.getState().prev();
      expect(useReplayStore.getState().index).toBe(0);
    });

    it("next increments index", () => {
      useReplayStore.getState().next();
      expect(useReplayStore.getState().index).toBe(2);
    });

    it("next does not exceed last entry", () => {
      useReplayStore.setState({ index: 3 });
      useReplayStore.getState().next();
      expect(useReplayStore.getState().index).toBe(3);
    });

    it("first goes to index 0", () => {
      useReplayStore.setState({ index: 3 });
      useReplayStore.getState().first();
      expect(useReplayStore.getState().index).toBe(0);
    });

    it("last goes to final entry", () => {
      useReplayStore.setState({ index: 0 });
      useReplayStore.getState().last();
      expect(useReplayStore.getState().index).toBe(3);
    });

    it("last returns 0 for empty entries", () => {
      useReplayStore.setState({ entries: [], index: 0 });
      useReplayStore.getState().last();
      expect(useReplayStore.getState().index).toBe(0);
    });

    it("setIndex clamps to valid range", () => {
      useReplayStore.getState().setIndex(100);
      expect(useReplayStore.getState().index).toBe(3);
    });

    it("setIndex clamps negative to 0", () => {
      useReplayStore.getState().setIndex(-5);
      expect(useReplayStore.getState().index).toBe(0);
    });
  });

  describe("setFilter / setLimit", () => {
    it("updates filter", () => {
      useReplayStore.getState().setFilter("input");
      expect(useReplayStore.getState().filter).toBe("input");
    });

    it("updates limit", () => {
      useReplayStore.getState().setLimit(500);
      expect(useReplayStore.getState().limit).toBe(500);
    });
  });

  describe("playback", () => {
    it("setPlaying starts playback", () => {
      useReplayStore.setState({ entries: MOCK_ENTRIES, index: 0 });
      useReplayStore.getState().setPlaying(true);
      expect(useReplayStore.getState().playing).toBe(true);
    });

    it("setPlaying false stops playback", () => {
      useReplayStore.setState({ entries: MOCK_ENTRIES, index: 0, playing: true });
      useReplayStore.getState().setPlaying(false);
      expect(useReplayStore.getState().playing).toBe(false);
    });

    it("advances index on timer tick", () => {
      useReplayStore.setState({ entries: MOCK_ENTRIES, index: 0 });
      useReplayStore.getState().setPlaying(true);

      // Advance past the computed delay (500ms delta / speed 1, capped at 2000ms)
      vi.advanceTimersByTime(600);
      expect(useReplayStore.getState().index).toBe(1);
    });

    it("stops at end of entries", () => {
      useReplayStore.setState({ entries: MOCK_ENTRIES, index: 2 });
      useReplayStore.getState().setPlaying(true);

      // Advance through remaining entries
      vi.advanceTimersByTime(5000);
      const state = useReplayStore.getState();
      expect(state.index).toBe(3);
      expect(state.playing).toBe(false);
    });

    it("stops when already at last entry", () => {
      useReplayStore.setState({ entries: MOCK_ENTRIES, index: 3 });
      useReplayStore.getState().setPlaying(true);
      // scheduleNext should immediately stop playing since we're at end
      expect(useReplayStore.getState().playing).toBe(false);
    });

    it("setSpeed updates speed", () => {
      useReplayStore.getState().setSpeed(2);
      expect(useReplayStore.getState().speed).toBe(2);
    });

    it("setSpeed restarts timer when playing", () => {
      useReplayStore.setState({ entries: MOCK_ENTRIES, index: 0 });
      useReplayStore.getState().setPlaying(true);
      useReplayStore.getState().setSpeed(4);
      expect(useReplayStore.getState().speed).toBe(4);
      // Timer is restarted with new speed; advance less time due to 4x speed
      vi.advanceTimersByTime(200);
      expect(useReplayStore.getState().index).toBe(1);
    });
  });
});
