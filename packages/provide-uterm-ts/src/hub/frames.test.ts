//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { encodeWorkerFrame, monoToWall } from "./index.ts";

interface HubFramesGolden {
  wall: number;
  mono: number;
  frames: Array<{ name: string; message: Record<string, unknown>; encoded: string }>;
  mono_to_wall: Array<{ mono: number | null; wall: number | null }>;
}

const golden = loadGolden<HubFramesGolden>("hub_frames_golden.json");

describe("encodeWorkerFrame", () => {
  it.each(golden.frames)("$name", (record) => {
    // The dispatch is the point: an input message goes out as raw terminal
    // data, everything else as a DLE/STX-framed control envelope. Sending a
    // control frame down the terminal path would feed JSON to the PTY.
    expect(encodeWorkerFrame(record.message)).toBe(record.encoded);
  });

  it("treats a missing, null, empty or non-string type as not-input", () => {
    // The reference coerces with str(msg.get("type") or ""), which folds all
    // four together — a port comparing the raw value would frame them wrong.
    const notInput = ["no type at all", "null type", "empty type", "non-string type"];
    for (const name of notInput) {
      const record = golden.frames.find((frame) => frame.name === name);
      expect(record?.encoded.startsWith("\x10\x02")).toBe(true);
    }
  });
});

describe("monoToWall", () => {
  it.each(golden.mono_to_wall)("converts $mono", (record) => {
    const clock = { wall: () => golden.wall, monotonic: () => golden.mono };
    expect(monoToWall(record.mono ?? undefined, clock)).toBe(record.wall ?? undefined);
  });

  it("passes an absent timestamp through rather than converting it", () => {
    const clock = { wall: () => golden.wall, monotonic: () => golden.mono };
    expect(monoToWall(undefined, clock)).toBeUndefined();
  });

  it("defaults to the real clocks", () => {
    // The offset between the two clocks is what matters, so a timestamp taken
    // now must convert to roughly now rather than to an epoch far away.
    const converted = monoToWall(performance.now() / 1000);
    expect(converted).toBeDefined();
    expect(Math.abs((converted ?? 0) - Date.now() / 1000)).toBeLessThan(5);
  });
});
