//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it, vi } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { Hijackable, STEP_TOKEN_CAP, STEP_TOKENS_PER_ITERATION } from "./index.ts";

interface HijackableGolden {
  step_token_cap: number;
  default_checkpoints: number;
  steps: Array<{ name: string; hijacked: boolean; requests: number[]; tokens: number }>;
  gate: {
    after_one_step: number;
    after_two_steps: number;
    blocks_without_tokens: boolean;
    tokens_after_resume: number;
    tokens_after_rehijack: number;
  };
}

const golden = loadGolden<HijackableGolden>("hijackable_golden.json");

/** Whether a promise settles within a turn of the event loop. */
async function settles(promise: Promise<void>): Promise<boolean> {
  return Promise.race([
    promise.then(() => true),
    new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 5)),
  ]);
}

describe("Hijackable step tokens", () => {
  it.each(golden.steps)("$name", async (record) => {
    // The arithmetic is bounded on purpose: unbounded accumulation from a
    // client hammering the step button would let the loop run away the
    // moment the hijack is released.
    const worker = new Hijackable();
    await worker.setHijacked(record.hijacked);
    if (record.requests.length === 0) {
      await worker.requestStep();
    }
    for (const amount of record.requests) {
      await worker.requestStep(amount);
    }
    expect(worker.stepTokens).toBe(record.tokens);
  });

  it("grants nothing while the worker is running freely", () => {
    // Stepping only means something against a paused loop; banking tokens
    // beforehand would let a later hijack be walked straight through.
    const record = golden.steps.find((entry) => entry.name === "not hijacked");
    expect(record?.tokens).toBe(0);
  });

  it("caps accumulation", () => {
    expect(STEP_TOKEN_CAP).toBe(golden.step_token_cap);
    expect(golden.steps.find((entry) => entry.name === "accumulating past the cap")?.tokens).toBe(STEP_TOKEN_CAP);
  });

  it("takes a negative request as nothing rather than as a debit", () => {
    // Otherwise a client could revoke credit it had already been granted,
    // or drive the count below zero and unblock the gate permanently.
    const record = golden.steps.find((entry) => entry.name === "negative after a grant keeps the grant");
    expect(record?.tokens).toBe(2);
  });

  it("defaults to a whole iteration", () => {
    // Two checkpoints — plan and act — so one press of Step advances the
    // loop exactly once.
    expect(STEP_TOKENS_PER_ITERATION).toBe(golden.default_checkpoints);
    expect(golden.steps.find((entry) => entry.name === "default request")?.tokens).toBe(STEP_TOKENS_PER_ITERATION);
  });

  it("truncates a fractional request", () => {
    const worker = new Hijackable();
    void worker.setHijacked(true);
    void worker.requestStep(2.9);
    expect(worker.stepTokens).toBe(2);
  });
});

describe("Hijackable checkpoint", () => {
  it("returns at once when the worker is not hijacked", async () => {
    const worker = new Hijackable();
    expect(await settles(worker.awaitIfHijacked())).toBe(true);
  });

  it("blocks while hijacked with no tokens", async () => {
    // The whole point: the automation stops where the operator put it.
    expect(golden.gate.blocks_without_tokens).toBe(true);
    const worker = new Hijackable();
    await worker.setHijacked(true);
    expect(await settles(worker.awaitIfHijacked())).toBe(false);
  });

  it("releases every waiter when the hijack ends", async () => {
    const worker = new Hijackable();
    await worker.setHijacked(true);
    const waiters = [worker.awaitIfHijacked(), worker.awaitIfHijacked()];
    await worker.setHijacked(false);
    expect(await settles(Promise.all(waiters).then(() => {}))).toBe(true);
  });

  it("spends one token per checkpoint", async () => {
    const worker = new Hijackable();
    await worker.setHijacked(true);
    await worker.requestStep(2);
    await worker.awaitIfHijacked();
    expect(worker.stepTokens).toBe(golden.gate.after_one_step);
    await worker.awaitIfHijacked();
    expect(worker.stepTokens).toBe(golden.gate.after_two_steps);
    expect(await settles(worker.awaitIfHijacked())).toBe(false);
  });

  it("keeps tokens across a resume but discards them on a new hijack", async () => {
    // Tokens granted for one hold must not silently apply to the next one,
    // or an operator pausing again would find the loop already walking.
    const worker = new Hijackable();
    await worker.setHijacked(true);
    await worker.requestStep(4);
    await worker.setHijacked(false);
    expect(worker.stepTokens).toBe(golden.gate.tokens_after_resume);
    await worker.setHijacked(true);
    expect(worker.stepTokens).toBe(golden.gate.tokens_after_rehijack);
  });

  it("is idempotent", async () => {
    const worker = new Hijackable();
    await worker.setHijacked(true);
    await worker.requestStep(2);
    await worker.setHijacked(true);
    // A repeated pause is not a new one, so the tokens survive.
    expect(worker.stepTokens).toBe(2);
    await worker.setHijacked(false);
    await worker.setHijacked(false);
    expect(await settles(worker.awaitIfHijacked())).toBe(true);
  });

  it("reports whether it is hijacked", async () => {
    const worker = new Hijackable();
    expect(worker.hijacked).toBe(false);
    await worker.setHijacked(true);
    expect(worker.hijacked).toBe(true);
  });
});

describe("Hijackable watchdog", () => {
  /** A worker whose clock and sleeps the test drives. */
  function build() {
    const clock = { now: 0 };
    const sleeps: number[] = [];
    let release: (() => void) | undefined;
    const worker = new Hijackable({
      now: () => clock.now,
      sleep: async (seconds) => {
        sleeps.push(seconds);
        clock.now += seconds;
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      },
    });
    return {
      worker,
      clock,
      sleeps,
      tick: async () => {
        release?.();
        await new Promise((resolve) => setTimeout(resolve, 0));
      },
    };
  }

  it("fires once the worker stops making progress", async () => {
    const onStuck = vi.fn(async () => {});
    const { worker, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6, onStuck });
    await tick();
    expect(onStuck).not.toHaveBeenCalled();
    await tick();
    expect(onStuck).toHaveBeenCalledOnce();
    await worker.stopWatchdog();
  });

  it("stays quiet while progress is being made", async () => {
    const onStuck = vi.fn(async () => {});
    const { worker, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6, onStuck });
    await tick();
    worker.noteProgress();
    await tick();
    expect(onStuck).not.toHaveBeenCalled();
    await worker.stopWatchdog();
  });

  it("suppresses itself while the worker is hijacked", async () => {
    // A paused worker is not a stuck one. Firing here would disconnect the
    // very session the operator is driving.
    const onStuck = vi.fn(async () => {});
    const { worker, tick } = build();
    await worker.setHijacked(true);
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6, onStuck });
    await tick();
    await tick();
    await tick();
    expect(onStuck).not.toHaveBeenCalled();
    await worker.stopWatchdog();
  });

  it("does not fire twice in a row for one stall", async () => {
    // It resets after firing, so a slow reconnect is not spammed with
    // repeat callbacks.
    const onStuck = vi.fn(async () => {});
    const { worker, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6, onStuck });
    await tick();
    await tick();
    await tick();
    expect(onStuck).toHaveBeenCalledOnce();
    await worker.stopWatchdog();
  });

  it("survives a callback that throws", async () => {
    const { worker, tick } = build();
    worker.startWatchdog({
      stuckTimeoutS: 10,
      checkIntervalS: 6,
      onStuck: async () => {
        throw new Error("reconnect failed");
      },
    });
    await tick();
    await expect(tick()).resolves.toBeUndefined();
    await worker.stopWatchdog();
  });

  it("runs without a callback", async () => {
    const { worker, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6 });
    await tick();
    await expect(tick()).resolves.toBeUndefined();
    await worker.stopWatchdog();
  });

  it("floors the check interval", async () => {
    // A zero interval would spin the event loop rather than watch anything.
    const { worker, sleeps, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 0 });
    await tick();
    expect(sleeps[0]).toBe(0.5);
    await worker.stopWatchdog();
  });

  it("ignores a second start", async () => {
    // Two watchdogs on one worker would double every check and race each
    // other's progress resets. The second call's interval must never appear.
    const { worker, sleeps, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6 });
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 1 });
    await tick();
    await tick();
    expect(new Set(sleeps)).toStrictEqual(new Set([6]));
    await worker.stopWatchdog();
  });

  it("can be restarted after being stopped", async () => {
    const { worker, sleeps, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6 });
    await tick();
    await worker.stopWatchdog();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 3 });
    await tick();
    expect(sleeps).toContain(3);
    await worker.stopWatchdog();
  });

  it("does not check again when stopped mid-sleep", async () => {
    // Stopping while a check is in flight must not let one more check land
    // afterwards — the caller is shutting down and expects no more callbacks.
    const onStuck = vi.fn(async () => {});
    const { worker, tick } = build();
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6, onStuck });
    await tick();
    await worker.stopWatchdog();
    await tick();
    await tick();
    expect(onStuck).not.toHaveBeenCalled();
  });

  it("waits on real time when given no sleep", async () => {
    // Every other test drives the clock; this one confirms the production
    // default actually waits rather than spinning.
    const worker = new Hijackable();
    const started = Date.now();
    worker.startWatchdog({ stuckTimeoutS: 3600, checkIntervalS: 0 });
    await new Promise((resolve) => setTimeout(resolve, 600));
    await worker.stopWatchdog();
    expect(Date.now() - started).toBeGreaterThanOrEqual(500);
  });

  it("stops idempotently", async () => {
    const worker = new Hijackable();
    await expect(worker.stopWatchdog()).resolves.toBeUndefined();
    await expect(worker.stopWatchdog()).resolves.toBeUndefined();
  });

  it("uses the reference defaults", async () => {
    const { worker, sleeps, tick } = build();
    worker.startWatchdog();
    await tick();
    expect(sleeps[0]).toBe(5);
    await worker.stopWatchdog();
  });
});

describe("Hijackable cleanup", () => {
  it("releases the hijack and stops the watchdog", async () => {
    // Shutdown must not leave a paused loop or a live timer behind.
    const worker = new Hijackable({ now: () => 0, sleep: async () => new Promise<void>(() => {}) });
    await worker.setHijacked(true);
    worker.startWatchdog({ stuckTimeoutS: 10, checkIntervalS: 6 });
    await worker.cleanupHijack();
    expect(worker.hijacked).toBe(false);
    expect(await settles(worker.awaitIfHijacked())).toBe(true);
  });
});
