//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, describe, expect, it, vi } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { autoDetectInputType, BufferManager } from "./index.ts";

interface DetectionGolden {
  input_type: Array<{ screen: string; type: string }>;
  buffers: Array<{
    name: string;
    snapshots: Array<Record<string, unknown>>;
    buffers: Array<{
      screen: string;
      screen_hash: string;
      captured_at: number;
      time_since_last_change: number;
      matched_prompt_id: string | null;
    }>;
    recent_2: string[];
    recent_all: string[];
  }>;
  eviction: { kept: string[] };
}

const golden = loadGolden<DetectionGolden>("detection_golden.json");

afterEach(() => {
  vi.useRealTimers();
});

describe("autoDetectInputType", () => {
  it.each([
    ["press any key to continue", "any_key"],
    ["<MORE>", "any_key"],
    ["-- More --", "any_key"],
  ])("classifies %j as any_key", (screen, expected) => {
    expect(autoDetectInputType(screen)).toBe(expected);
  });

  it.each([
    ["Continue? (y/n)", "single_key"],
    ["Proceed [Y/N]", "single_key"],
    ["(Q)uit", "single_key"],
    ["abort?", "single_key"],
  ])("classifies %j as single_key", (screen, expected) => {
    expect(autoDetectInputType(screen)).toBe(expected);
  });

  it.each([
    ["Enter your name", "multi_key"],
    ["Password:", "multi_key"],
    ["Command:", "multi_key"],
  ])("classifies %j as multi_key", (screen, expected) => {
    expect(autoDetectInputType(screen)).toBe(expected);
  });

  it("falls through to multi_key when no phrase matches", () => {
    expect(autoDetectInputType("")).toBe("multi_key");
    expect(autoDetectInputType("1234567890")).toBe("multi_key");
  });

  it("matches case-insensitively and anywhere in the screen", () => {
    expect(autoDetectInputType("PRESS ANY KEY")).toBe("any_key");
    expect(autoDetectInputType("xxpress any keyxx")).toBe("any_key");
  });

  it("resolves to the earlier tier when tiers overlap", () => {
    // "press any key" and "enter" are both present; the any_key tier is
    // checked first, so the later phrase never gets a say.
    expect(autoDetectInputType("Press any key, then enter your name")).toBe("any_key");
    expect(autoDetectInputType("Continue? (y/n) — or type a command")).toBe("single_key");
  });
});

describe("BufferManager", () => {
  it("reports no elapsed time for the first screen", () => {
    const manager = new BufferManager();
    const buffer = manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 100 });
    expect(buffer.time_since_last_change).toBe(0);
  });

  it("accumulates elapsed time while the screen is unchanged", () => {
    const manager = new BufferManager();
    manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 100 });
    expect(manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 102.5 }).time_since_last_change).toBe(2.5);
    expect(manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 105 }).time_since_last_change).toBe(5);
  });

  it("restarts the clock when the screen changes", () => {
    const manager = new BufferManager();
    manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 100 });
    manager.addScreen({ screen: "b", screen_hash: "h2", captured_at: 103 });
    expect(manager.addScreen({ screen: "b", screen_hash: "h2", captured_at: 104 }).time_since_last_change).toBe(1);
  });

  it("stamps the current time when a snapshot carries none", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_700_000_000_000));
    const manager = new BufferManager();
    expect(manager.addScreen({ screen: "a", screen_hash: "h1" }).captured_at).toBe(1_700_000_000);
  });

  it("returns the most recent screens, oldest first", () => {
    const manager = new BufferManager();
    for (const i of [1, 2, 3]) {
      manager.addScreen({ screen: String(i), screen_hash: `h${i}`, captured_at: i });
    }
    expect(manager.getRecent(2).map((b) => b.screen_hash)).toStrictEqual(["h2", "h3"]);
  });

  it("returns everything when asked for more than it holds", () => {
    const manager = new BufferManager();
    manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 1 });
    expect(manager.getRecent(99)).toHaveLength(1);
  });

  it("evicts the oldest screen past its bound", () => {
    const manager = new BufferManager(3);
    for (let i = 0; i < 5; i += 1) {
      manager.addScreen({ screen: String(i), screen_hash: `h${i}`, captured_at: i });
    }
    expect(manager.getRecent(99).map((b) => b.screen_hash)).toStrictEqual(golden.eviction.kept);
  });

  it("reports idle only once the screen has been stable for the threshold", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_000_000));
    const manager = new BufferManager();
    manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 1000 });
    vi.setSystemTime(new Date(1_001_500));
    expect(manager.detectIdleState(2)).toBe(false);
    vi.setSystemTime(new Date(1_003_000));
    expect(manager.detectIdleState(2)).toBe(true);
  });

  it("is never idle before the first screen arrives", () => {
    expect(new BufferManager().detectIdleState(0)).toBe(false);
  });

  it("resets its state on clear", () => {
    const manager = new BufferManager();
    manager.addScreen({ screen: "a", screen_hash: "h1", captured_at: 100 });
    manager.clear();
    expect(manager.getRecent(99)).toStrictEqual([]);
    expect(manager.detectIdleState(0)).toBe(false);
    // The clock restarted, so the next screen reports no elapsed time.
    expect(manager.addScreen({ screen: "b", screen_hash: "h2", captured_at: 200 }).time_since_last_change).toBe(0);
  });
});

describe("differential parity with CPython", () => {
  it("matches every input-type classification", () => {
    for (const record of golden.input_type) {
      expect({ screen: record.screen, type: autoDetectInputType(record.screen) }).toStrictEqual(record);
    }
    expect(golden.input_type.length).toBeGreaterThan(30);
  });

  it("matches every buffered screen and its timing", () => {
    for (const record of golden.buffers) {
      const manager = new BufferManager(3);
      const produced = record.snapshots.map((snapshot) => {
        const buffer = manager.addScreen(snapshot as Parameters<BufferManager["addScreen"]>[0]);
        return {
          screen: buffer.screen,
          screen_hash: buffer.screen_hash,
          captured_at: buffer.captured_at,
          time_since_last_change: buffer.time_since_last_change,
          matched_prompt_id: buffer.matched_prompt_id,
        };
      });
      expect({
        name: record.name,
        buffers: produced,
        recent_2: manager.getRecent(2).map((b) => b.screen_hash),
        recent_all: manager.getRecent(99).map((b) => b.screen_hash),
      }).toStrictEqual({
        name: record.name,
        buffers: record.buffers,
        recent_2: record.recent_2,
        recent_all: record.recent_all,
      });
    }
    expect(golden.buffers.length).toBeGreaterThan(3);
  });
});
