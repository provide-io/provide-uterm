//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { FlowController, PAUSE, RESUME } from "./index.ts";

interface FlowState {
  paused: boolean;
  max_inflight: number;
  all_active_congested: boolean;
  congested: Record<string, boolean>;
}

interface FlowGolden {
  high_water: number;
  low_water: number;
  ack_grace_s: number;
  pause: string;
  resume: string;
  monotonic_acks: { after_first: number; after_stale: number; after_newer: number };
  silent_client: {
    still_congested: boolean;
    max_inflight_ignores_it: number;
    all_active_congested: boolean;
    decision: null;
    inside_the_window: boolean;
    on_the_boundary: boolean;
    just_past_the_boundary: boolean;
  };
  no_ackers: { all_active_congested: boolean; decision: null; max_inflight: number; is_congested: boolean };
  hysteresis: Array<FlowState & { name: string; inflight: number }>;
  fairness: {
    one_congested: {
      all_active_congested: boolean;
      decision: null;
      slow_is_congested: boolean;
      fast_is_congested: boolean;
    };
    both_congested: { all_active_congested: boolean; decision: string; paused: boolean };
    recovered: { all_active_congested: boolean; decision: string; paused: boolean };
  };
  recovered: { before: string[]; after: string[]; again: string[] };
  forget: { before: FlowState; after: FlowState; forget_unknown_raises: boolean };
  decisions: Array<{ name: string; decision: string | null; paused: boolean }>;
}

const golden = loadGolden<FlowGolden>("cfflow_golden.json");

const HIGH = golden.high_water;
const LOW = golden.low_water;
const GRACE = golden.ack_grace_s;

/** A controller with the corpus's own thresholds. */
function controller(): FlowController {
  return new FlowController({ highWater: HIGH, lowWater: LOW, ackGraceS: GRACE });
}

/** Everything observable about a controller. */
function state(subject: FlowController, now: number, ids: string[]): FlowState {
  return {
    paused: subject.paused,
    max_inflight: subject.maxInflight(now),
    all_active_congested: subject.allActiveCongested(now),
    congested: Object.fromEntries(ids.map((id) => [id, subject.isCongested(id)])),
  };
}

describe("what a browser says it has consumed", () => {
  it("only ever moves forwards", () => {
    // These are cumulative counts. A stale or replayed one carrying a lower
    // number would rewind what a browser is known to have consumed, and
    // invent congestion that is not there.
    const subject = controller();
    subject.onSent("a", 500);
    subject.onAck("a", 400, 1.0);
    expect(subject.maxInflight(1.0)).toBe(golden.monotonic_acks.after_first);
    subject.onAck("a", 100, 2.0);
    expect(subject.maxInflight(2.0)).toBe(golden.monotonic_acks.after_stale);
    subject.onAck("a", 500, 3.0);
    expect(subject.maxInflight(3.0)).toBe(golden.monotonic_acks.after_newer);
  });

  it("copes with an acknowledgement before anything was sent", () => {
    // A browser may acknowledge as it connects, before this has sent it
    // anything. Reading that as a negative backlog would make it look
    // permanently ahead.
    const subject = controller();
    subject.onAck("a", 0, 1.0);
    expect(subject.maxInflight(1.0)).toBe(0);
    expect(subject.isCongested("a")).toBe(false);
    expect(subject.allActiveCongested(1.0)).toBe(false);
  });

  it("accumulates what was sent", () => {
    const subject = controller();
    subject.onSent("a", 100);
    subject.onSent("a", 50);
    subject.onAck("a", 0, 1.0);
    expect(subject.maxInflight(1.0)).toBe(150);
  });
});

describe("a browser that has gone quiet", () => {
  it("is left out of the decision", () => {
    // The trap the whole design is built around: a client that stops
    // acknowledging looks maximally congested for ever, and counting it would
    // pause the producer permanently for one stuck tab.
    const subject = controller();
    subject.onSent("stuck", HIGH + 1);
    subject.onAck("stuck", 0, 0.0);
    const now = GRACE + 5;
    expect(subject.isCongested("stuck")).toBe(golden.silent_client.still_congested);
    expect(subject.maxInflight(now)).toBe(golden.silent_client.max_inflight_ignores_it);
    expect(subject.allActiveCongested(now)).toBe(golden.silent_client.all_active_congested);
    expect(subject.decide(now)).toBeUndefined();
  });

  it("counts up to and including the grace window", () => {
    // A browser exactly at the limit has not yet gone quiet.
    const subject = controller();
    subject.onSent("stuck", HIGH + 1);
    subject.onAck("stuck", 0, 0.0);
    expect(subject.allActiveCongested(GRACE)).toBe(golden.silent_client.on_the_boundary);
    expect(subject.allActiveCongested(GRACE + 0.001)).toBe(golden.silent_client.just_past_the_boundary);
  });

  it("never pauses a session where nobody has acknowledged at all", () => {
    // Best effort: a producer with no feedback keeps producing rather than
    // stalling on a guess.
    const subject = controller();
    subject.onSent("a", HIGH * 10);
    expect(subject.allActiveCongested(1.0)).toBe(golden.no_ackers.all_active_congested);
    expect(subject.decide(1.0)).toBeUndefined();
    expect(subject.maxInflight(1.0)).toBe(golden.no_ackers.max_inflight);
  });

  it("still marks it congested for its own frames", () => {
    // Congestion decides whether droppable frames are sent to *that* browser,
    // which is a separate question from whether the producer should pause.
    const subject = controller();
    subject.onSent("a", HIGH * 10);
    expect(subject.isCongested("a")).toBe(golden.no_ackers.is_congested);
  });
});

describe("becoming and stopping being congested", () => {
  it.each(golden.hysteresis)("$name", (record) => {
    // Replayed as a walk, because the state is sticky: each step's answer
    // depends on the ones before it.
    const subject = controller();
    let sent = 0;
    let acked = 0;
    for (const step of golden.hysteresis) {
      const target = acked + step.inflight;
      if (target > sent) {
        subject.onSent("a", target - sent);
        sent = target;
      } else {
        acked = sent - step.inflight;
      }
      subject.onAck("a", acked, step.name === record.name ? 1.0 : 1.0);
      if (step.name === record.name) {
        expect(state(subject, 1.0, ["a"])).toStrictEqual({
          paused: record.paused,
          max_inflight: record.max_inflight,
          all_active_congested: record.all_active_congested,
          congested: record.congested,
        });
        return;
      }
    }
  });

  it("sets above the high mark, not on it", () => {
    const walk = golden.hysteresis;
    expect(walk.find((step) => step.name === "exactly on the high mark")?.congested.a).toBe(false);
    expect(walk.find((step) => step.name === "just above the high mark")?.congested.a).toBe(true);
  });

  it("clears below the low mark, not on it", () => {
    // The gap between the marks is what stops it flapping either side of a
    // single threshold, pausing and resuming on every frame.
    const walk = golden.hysteresis;
    expect(walk.find((step) => step.name === "exactly on the low mark")?.congested.a).toBe(true);
    expect(walk.find((step) => step.name === "just below the low mark")?.congested.a).toBe(false);
  });

  it("stays congested all the way down between the marks", () => {
    expect(golden.hysteresis.find((step) => step.name === "draining, still above the low mark")?.congested.a).toBe(
      true,
    );
  });

  it("can become congested again after recovering", () => {
    expect(golden.hysteresis.at(-1)?.congested.a).toBe(true);
  });
});

describe("one slow browser among several", () => {
  it("does not pause the producer on its own", () => {
    // If even the fastest consumer can keep up there is something worth
    // producing. Pausing on the slowest would let one browser throttle
    // everybody else's session.
    const subject = controller();
    subject.onSent("slow", HIGH + 1);
    subject.onAck("slow", 0, 1.0);
    subject.onSent("fast", 10);
    subject.onAck("fast", 10, 1.0);
    expect(subject.allActiveCongested(1.0)).toBe(golden.fairness.one_congested.all_active_congested);
    expect(subject.decide(1.0)).toBeUndefined();
    expect(subject.isCongested("slow")).toBe(true);
    expect(subject.isCongested("fast")).toBe(false);
  });

  it("pauses once every browser is behind", () => {
    const subject = controller();
    subject.onSent("slow", HIGH + 1);
    subject.onAck("slow", 0, 1.0);
    subject.onSent("fast", 10);
    subject.onAck("fast", 10, 1.0);
    subject.decide(1.0);
    subject.onSent("fast", HIGH + 1);
    expect(subject.decide(1.0)).toBe(PAUSE);
    expect(subject.paused).toBe(true);
  });

  it("resumes as soon as one can keep up again", () => {
    const subject = controller();
    subject.onSent("slow", HIGH + 1);
    subject.onAck("slow", 0, 1.0);
    subject.onSent("fast", HIGH + 11);
    subject.onAck("fast", 10, 1.0);
    expect(subject.decide(1.0)).toBe(PAUSE);
    subject.onAck("fast", HIGH + 11, 2.0);
    expect(subject.decide(2.0)).toBe(RESUME);
    expect(subject.paused).toBe(false);
  });
});

describe("telling the producer", () => {
  it("reports a change once, not on every check", () => {
    // A producer told to pause repeatedly would either act on it repeatedly
    // or learn to ignore it.
    const subject = controller();
    subject.onSent("a", HIGH + 1);
    subject.onAck("a", 0, 1.0);
    expect(subject.decide(1.0)).toBe(PAUSE);
    expect(subject.decide(1.0)).toBeUndefined();
    subject.onAck("a", HIGH + 1, 2.0);
    expect(subject.decide(2.0)).toBe(RESUME);
    expect(subject.decide(2.0)).toBeUndefined();
  });

  it("matches the recorded sequence", () => {
    expect(golden.decisions.map((entry) => entry.decision)).toStrictEqual([PAUSE, null, RESUME, null]);
    expect(golden.pause).toBe(PAUSE);
    expect(golden.resume).toBe(RESUME);
  });

  it("starts unpaused", () => {
    expect(controller().paused).toBe(false);
  });
});

describe("browsers that catch up", () => {
  it("names each one once", () => {
    // It missed frames while congested, so it needs a fresh snapshot —
    // reporting it twice would send two.
    const subject = controller();
    subject.onSent("a", HIGH + 1);
    subject.onAck("a", 0, 1.0);
    expect([...subject.takeRecovered()].sort()).toStrictEqual(golden.recovered.before);
    subject.onAck("a", HIGH + 1, 2.0);
    expect([...subject.takeRecovered()].sort()).toStrictEqual(golden.recovered.after);
    expect([...subject.takeRecovered()].sort()).toStrictEqual(golden.recovered.again);
  });

  it("names one that never congested not at all", () => {
    // A browser that kept up has missed nothing and needs no snapshot.
    const subject = controller();
    subject.onSent("a", 10);
    subject.onAck("a", 10, 1.0);
    expect([...subject.takeRecovered()]).toStrictEqual([]);
  });

  it("collects several", () => {
    const subject = controller();
    for (const id of ["a", "b"]) {
      subject.onSent(id, HIGH + 1);
      subject.onAck(id, 0, 1.0);
      subject.onAck(id, HIGH + 1, 2.0);
    }
    expect([...subject.takeRecovered()].sort()).toStrictEqual(["a", "b"]);
  });
});

describe("a browser that disconnects", () => {
  it("leaves nothing behind", () => {
    // Its outstanding bytes would otherwise keep the producer paused for a
    // tab that has already closed.
    const subject = controller();
    subject.onSent("a", HIGH + 1);
    subject.onAck("a", 0, 1.0);
    subject.onSent("b", 10);
    subject.onAck("b", 10, 1.0);
    expect(state(subject, 1.0, ["a", "b"])).toStrictEqual(golden.forget.before);
    subject.forget("a");
    expect(state(subject, 1.0, ["a", "b"])).toStrictEqual(golden.forget.after);
  });

  it("leaves the others alone", () => {
    // Forgetting is per browser. Clearing everything would zero the accounting
    // for every tab still watching, and the producer would resume into a
    // backlog it had just been told about.
    const subject = controller();
    subject.onSent("a", HIGH + 1);
    subject.onAck("a", 0, 1.0);
    subject.onSent("b", 500);
    subject.onAck("b", 100, 1.0);
    expect(subject.maxInflight(1.0)).toBe(HIGH + 1);
    subject.forget("a");
    expect(subject.maxInflight(1.0)).toBe(400);
    expect(subject.isCongested("b")).toBe(false);
  });

  it("starts a browser that reconnects from nothing", () => {
    // The same tab id may come back. Its old sent count would look like a
    // backlog it never received.
    const subject = controller();
    subject.onSent("a", HIGH + 1);
    subject.onAck("a", 0, 1.0);
    subject.forget("a");
    subject.onSent("a", 10);
    subject.onAck("a", 0, 2.0);
    expect(subject.maxInflight(2.0)).toBe(10);
    expect(subject.isCongested("a")).toBe(false);
  });

  it("takes its pending recovery with it", () => {
    const subject = controller();
    subject.onSent("a", HIGH + 1);
    subject.onAck("a", 0, 1.0);
    subject.onAck("a", HIGH + 1, 2.0);
    subject.forget("a");
    expect([...subject.takeRecovered()]).toStrictEqual([]);
  });

  it("copes with one it never knew", () => {
    expect(() => controller().forget("never-seen")).not.toThrow();
    expect(golden.forget.forget_unknown_raises).toBe(false);
  });
});
