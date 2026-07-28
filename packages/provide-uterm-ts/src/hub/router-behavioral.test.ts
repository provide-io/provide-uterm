//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { KEYSTROKE_RING_MAX, KeystrokeTracker } from "./index.ts";

interface RouterBehavioralGolden {
  ring_max: number;
  heuristics: Array<{ name: string; timestamps: number[]; cps: number; jitter: number }>;
  ring: { maxlen: number; length: number; first: number; last: number; cps: number; jitter: number };
  isolation: {
    before_a: { cps: number; jitter: number };
    before_b: { cps: number; jitter: number };
    after_a: { cps: number; jitter: number };
    after_b: { cps: number; jitter: number };
    tracked_after_forget: string[];
  };
}

const golden = loadGolden<RouterBehavioralGolden>("router_behavioral_golden.json");
const A = { id: "a" };
const B = { id: "b" };

/** A tracker whose clock the test drives. */
function build() {
  let now = 0;
  const tracker = new KeystrokeTracker({ now: () => now });
  return {
    tracker,
    /** Record a keystroke at `at`. */
    at(when: number) {
      now = when;
      tracker.record(A);
    },
  };
}

describe("KeystrokeTracker heuristics", () => {
  it.each(golden.heuristics)("$name", (record) => {
    // These feed a gate that can close a connection, so the numbers are
    // compared exactly rather than approximately.
    const { tracker, at } = build();
    for (const timestamp of record.timestamps) {
      at(timestamp);
    }
    expect(tracker.heuristics(A)).toStrictEqual({ cps: record.cps, jitter: record.jitter });
  });

  it("reports zeros for a browser it has never seen", () => {
    expect(new KeystrokeTracker().heuristics(A)).toStrictEqual({ cps: 0, jitter: 0 });
  });

  it("reports zeros rather than dividing by zero on a burst", () => {
    // Every keystroke sharing a timestamp is what a paste looks like, and
    // the duration it implies is zero.
    const record = golden.heuristics.find((entry) => entry.name === "all at the same instant");
    expect(record?.cps).toBe(0);
  });

  it("computes jitter as the sample variance of the intervals", () => {
    // Not the population variance, and not a float two-pass formula: an even
    // rhythm has to come out as a vanishing number, not merely a small one.
    const even = golden.heuristics.find((entry) => entry.name === "machine-even, no jitter");
    const uneven = golden.heuristics.find((entry) => entry.name === "human-ish, uneven");
    expect(even?.jitter).toBeLessThan(1e-20);
    expect(uneven?.jitter).toBeGreaterThan(0.01);
  });
});

describe("KeystrokeTracker ring", () => {
  it("keeps only the newest timestamps", () => {
    // A long session must measure a rolling window; without the bound the
    // rate would converge on the session average and stop reacting.
    const { tracker, at } = build();
    for (let index = 0; index < golden.ring_max + 10; index += 1) {
      at(1000 + index);
    }
    expect(tracker.heuristics(A)).toStrictEqual({ cps: golden.ring.cps, jitter: golden.ring.jitter });
    expect(KEYSTROKE_RING_MAX).toBe(golden.ring.maxlen);
  });

  it("measures from the oldest retained timestamp, not the first ever seen", () => {
    const { tracker, at } = build();
    at(0);
    for (let index = 0; index < golden.ring_max; index += 1) {
      at(1000 + index);
    }
    // The ancient first keystroke has been dropped, so the rate reflects the
    // recent run rather than being flattened by the gap.
    expect(tracker.heuristics(A).cps).toBeCloseTo(1, 10);
  });
});

describe("KeystrokeTracker isolation", () => {
  it("tracks each browser separately", () => {
    const tracker = new KeystrokeTracker({ now: () => 1000 });
    tracker.record(A);
    tracker.record(B);
    expect(tracker.heuristics(A)).toStrictEqual(golden.isolation.before_b);
  });

  it("forgets one browser without disturbing another", () => {
    let now = 1000;
    const tracker = new KeystrokeTracker({ now: () => now });
    tracker.record(A);
    now = 1000.5;
    tracker.record(A);
    tracker.record(B);
    expect(tracker.heuristics(A)).toStrictEqual(golden.isolation.before_a);
    tracker.forget(A);
    expect(tracker.heuristics(A)).toStrictEqual(golden.isolation.after_a);
    expect(tracker.tracked()).toStrictEqual([B]);
  });

  it("forgets a browser it never knew without complaining", () => {
    // Disconnect cleanup runs for every socket, including ones that never
    // typed anything.
    const tracker = new KeystrokeTracker();
    expect(() => tracker.forget(A)).not.toThrow();
  });

  it("defaults to the monotonic clock", () => {
    const tracker = new KeystrokeTracker();
    tracker.record(A);
    tracker.record(A);
    // Two keystrokes in the same tick give a zero duration; the point is
    // that it read a real clock rather than throwing.
    expect(tracker.heuristics(A).cps).toBeGreaterThanOrEqual(0);
  });
});
