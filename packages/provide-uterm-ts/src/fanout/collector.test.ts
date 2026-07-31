//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { collectOutput, type EventSubscription, openOutputCapture } from "./index.ts";

/**
 * A subscription the test drives step by step.
 *
 * Each scripted entry is either an event, a pause of some length, or the
 * disconnect sentinel. Time only moves when the collector waits, so what is
 * under test is the decision to stop — not how fast the machine is.
 */
class ScriptedSubscription implements EventSubscription {
  closed = false;
  #index = 0;

  readonly #script: Array<{ event: Record<string, unknown> } | { silenceMs: number } | { disconnect: true }>;
  readonly #clock: { now: number };

  constructor(
    script: Array<{ event: Record<string, unknown> } | { silenceMs: number } | { disconnect: true }>,
    clock: { now: number },
  ) {
    this.#script = script;
    this.#clock = clock;
  }

  /** Wait up to `timeoutMs` for the next event. */
  async next(timeoutMs: number): Promise<Record<string, unknown> | undefined | null> {
    const step = this.#script[this.#index];
    if (step === undefined) {
      // Nothing left to say: behave like a silent stream.
      this.#clock.now += timeoutMs;
      return undefined;
    }
    this.#index += 1;
    if ("silenceMs" in step) {
      if (step.silenceMs >= timeoutMs) {
        this.#clock.now += timeoutMs;
        return undefined;
      }
      this.#clock.now += step.silenceMs;
      return this.next(timeoutMs - step.silenceMs);
    }
    if ("disconnect" in step) {
      return null;
    }
    return step.event;
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}

/** Run the collector over a script, with a clock the script advances. */
async function collect(
  script: Array<{ event: Record<string, unknown> } | { silenceMs: number } | { disconnect: true }>,
  options: { quiesceMs?: number; maxMs?: number } = {},
) {
  const clock = { now: 0 };
  const subscription = new ScriptedSubscription(script, clock);
  const result = await collectOutput({
    subscribe: async () => subscription,
    now: () => clock.now / 1000,
    quiesceMs: options.quiesceMs ?? 500,
    maxMs: options.maxMs ?? 10_000,
  });
  return { ...result, subscription };
}

/** A term event carrying `data`. */
function term(data: unknown) {
  return { event: { type: "term", data: { data } } };
}

/** A snapshot event carrying `screen`. */
function snapshot(screen: unknown) {
  return { event: { type: "snapshot", data: { screen } } };
}

describe("collectOutput", () => {
  it("closes an explicitly opened capture exactly once", async () => {
    let closes = 0;
    const capture = await openOutputCapture({
      subscribe: async () => ({
        next: async () => undefined,
        close: async () => {
          closes += 1;
        },
      }),
    });

    await capture?.close();
    await capture?.close();

    expect(closes).toBe(1);
  });
  it("returns nothing when there is no event bus", async () => {
    // A hub without one is a valid configuration, not an error.
    expect(await collectOutput({ quiesceMs: 500, maxMs: 10_000 })).toStrictEqual({ output: "", elapsedMs: 0 });
  });

  it("concatenates term deltas in order", async () => {
    const { output } = await collect([term("one "), term("two "), term("three")]);
    expect(output).toBe("one two three");
  });

  it("stops once the stream goes quiet", async () => {
    // The adaptive part: a session that has finished talking should not cost
    // the caller the full hard cap.
    const { output, elapsedMs } = await collect([term("done"), { silenceMs: 5000 }], {
      quiesceMs: 500,
      maxMs: 10_000,
    });
    expect(output).toBe("done");
    expect(elapsedMs).toBe(500);
  });

  it("gives up at the hard cap on a session that never stops", async () => {
    // A command that streams forever must not hold the fan-out open.
    const chatty = Array.from({ length: 100 }, () => term("x"));
    const { output, elapsedMs } = await collect([...chatty, { silenceMs: 100 }], { quiesceMs: 200, maxMs: 300 });
    expect(output.length).toBeGreaterThan(0);
    expect(elapsedMs).toBeLessThanOrEqual(300);
  });

  it("stops when the worker disconnects", async () => {
    // Waiting out the quiesce window for a session that has gone is pure
    // latency; the sentinel says so immediately.
    const { output, elapsedMs } = await collect([term("partial"), { disconnect: true }], { quiesceMs: 5000 });
    expect(output).toBe("partial");
    expect(elapsedMs).toBe(0);
  });

  it("falls back to the last snapshot when no term events arrive", async () => {
    // Snapshot-only connectors — the shell and SSH control paths — never emit
    // term deltas, and returning nothing for them would make every fan-out
    // over those sessions look empty.
    const { output } = await collect([snapshot("first screen"), snapshot("second screen")]);
    expect(output).toBe("second screen");
  });

  it("prefers term deltas over snapshots when both arrive", async () => {
    const { output } = await collect([snapshot("whole screen"), term("just the delta")]);
    expect(output).toBe("just the delta");
  });

  it("ignores an empty term delta", async () => {
    const { output } = await collect([term(""), snapshot("screen")]);
    expect(output).toBe("screen");
  });

  it("ignores an empty snapshot screen", async () => {
    const { output } = await collect([snapshot("real"), snapshot("")]);
    expect(output).toBe("real");
  });

  it("ignores an event whose payload is not an object", async () => {
    // Events come off a bus shared with other producers; a malformed one
    // must not throw in the middle of a fan-out.
    const { output } = await collect([{ event: { type: "term", data: "not an object" } }, term("ok")]);
    expect(output).toBe("ok");
  });

  it("ignores a payload field that is not text", async () => {
    // A term delta that arrives as a number is malformed, not output; using
    // it would splice a bare digit into the session's transcript.
    const { output } = await collect([term(42), snapshot(["not", "text"]), term("ok")]);
    expect(output).toBe("ok");
  });

  it("ignores a null payload", async () => {
    // typeof null is "object", so the guard has to name it explicitly.
    const { output } = await collect([{ event: { type: "term", data: null } }, term("ok")]);
    expect(output).toBe("ok");
  });

  it("ignores an event with no payload at all", async () => {
    const { output } = await collect([{ event: { type: "term" } }, term("ok")]);
    expect(output).toBe("ok");
  });

  it("treats a non-term event as a snapshot", async () => {
    // The subscription filters to term and snapshot, so anything that is not
    // term is the snapshot path.
    const { output } = await collect([{ event: { type: "snapshot", data: { screen: "s" } } }]);
    expect(output).toBe("s");
  });

  it("closes its subscription", async () => {
    // The bus keeps a queue per subscriber; leaving one open leaks it for
    // the life of the worker.
    const { subscription } = await collect([term("x"), { silenceMs: 5000 }]);
    expect(subscription.closed).toBe(true);
  });

  it("closes its subscription even when collection throws", async () => {
    const subscription = {
      closed: false,
      async next(): Promise<never> {
        throw new Error("bus exploded");
      },
      async close() {
        this.closed = true;
      },
    };
    await expect(collectOutput({ subscribe: async () => subscription, quiesceMs: 10, maxMs: 20 })).rejects.toThrow(
      "bus exploded",
    );
    expect(subscription.closed).toBe(true);
  });

  it("returns immediately when the cap is already spent", async () => {
    const { output, elapsedMs } = await collect([term("never read")], { maxMs: 0 });
    expect(output).toBe("");
    expect(elapsedMs).toBe(0);
  });

  it("waits no longer than the remaining budget", async () => {
    // The per-wait timeout is the smaller of the quiesce window and what is
    // left of the cap, so a long quiesce cannot overshoot the hard limit.
    const { elapsedMs } = await collect([{ silenceMs: 10_000 }], { quiesceMs: 5000, maxMs: 200 });
    expect(elapsedMs).toBe(200);
  });
});
