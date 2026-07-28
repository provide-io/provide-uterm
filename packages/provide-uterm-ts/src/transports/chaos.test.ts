//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type ChaosOptions, ChaosTransport, type ConnectionTransport, TransportConnectionError } from "./index.ts";

interface ChaosGolden {
  receives: number;
  schedules: Array<{
    name: string;
    outcomes: Array<{
      kind: "data" | "timeout" | "disconnect";
      data?: string;
      message?: string;
      connected_after?: boolean;
    }>;
  }>;
  delegation: {
    calls: unknown[][];
    connected_before: boolean;
    connected_after_connect: boolean;
    connected_after_disconnect: boolean;
  };
  jitter: {
    jittered_count: number;
    jitter_bound_s: number;
    jitter_within_bound: boolean;
    unjittered_count: number;
    timeout_delays_s: number[];
  };
  defaults: {
    seed: number;
    label: string;
    disconnect_every_n_receives: number;
    timeout_every_n_receives: number;
    max_jitter_ms: number;
  };
}

const golden = loadGolden<ChaosGolden>("chaos_golden.json");

/** The options for a recorded schedule, in the port's spelling. */
function optionsFor(name: string): ChaosOptions {
  const cases: Record<string, ChaosOptions> = {
    "no faults": {},
    "disconnect every third": { disconnectEveryNReceives: 3 },
    "timeout every second": { timeoutEveryNReceives: 2 },
    "disconnect every one": { disconnectEveryNReceives: 1 },
    "both, disconnect wins the tie": { disconnectEveryNReceives: 2, timeoutEveryNReceives: 2 },
    "both, out of phase": { disconnectEveryNReceives: 3, timeoutEveryNReceives: 2 },
    "a custom label": { disconnectEveryNReceives: 2, label: "flaky-bbs" },
    "an empty label falls back": { disconnectEveryNReceives: 2, label: "" },
  };
  return cases[name] as ChaosOptions;
}

/** Records what the wrapper delegated, and returns identifiable reads. */
class FakeInner implements ConnectionTransport {
  readonly calls: unknown[][] = [];
  connected = false;
  reads = 0;

  async connect(host: string, port: number, options: Record<string, unknown> = {}): Promise<void> {
    this.calls.push(["connect", host, port, Object.keys(options).sort()]);
    this.connected = true;
  }

  async disconnect(): Promise<void> {
    this.calls.push(["disconnect"]);
    this.connected = false;
  }

  async send(data: Uint8Array): Promise<void> {
    this.calls.push(["send", [...data]]);
  }

  async receive(maxBytes: number, timeoutMs: number): Promise<Uint8Array> {
    this.reads += 1;
    this.calls.push(["receive", maxBytes, timeoutMs]);
    return new TextEncoder().encode(`read-${this.reads}`);
  }

  isConnected(): boolean {
    return this.connected;
  }
}

/** Records what it was asked to sleep for, without sleeping. */
function recorder() {
  const slept: number[] = [];
  return { slept, sleep: async (seconds: number) => void slept.push(seconds) };
}

/** Drive `count` reads and describe what each one did. */
async function driveReads(chaos: ChaosTransport, count: number) {
  const outcomes = [];
  for (let index = 0; index < count; index += 1) {
    try {
      const data = await chaos.receive(4096, 250);
      outcomes.push({
        kind: data.length > 0 ? "data" : "timeout",
        data: new TextDecoder().decode(data),
      });
    } catch (error) {
      outcomes.push({
        kind: "disconnect",
        message: (error as Error).message,
        connected_after: chaos.isConnected(),
      });
    }
  }
  return outcomes;
}

describe("the fault schedule", () => {
  it.each(golden.schedules)("$name", async (record) => {
    // The wrapper exists so resilience can be tested against a schedule
    // rather than against luck — which read drops is the whole contract.
    const { sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { ...optionsFor(record.name), sleep });
    await chaos.connect("h", 1);
    const outcomes = await driveReads(chaos, golden.receives);
    expect(outcomes).toStrictEqual(
      record.outcomes.map((outcome) =>
        outcome.kind === "disconnect"
          ? { kind: "disconnect", message: outcome.message, connected_after: outcome.connected_after }
          : { kind: outcome.kind, data: outcome.data },
      ),
    );
  });

  it("counts from one, so the first read is never the Nth", async () => {
    // Counting from zero would make every session fail on its very first
    // read, which is not the fault being modelled.
    const record = golden.schedules.find((entry) => entry.name === "disconnect every third");
    expect(record?.outcomes[0]?.kind).toBe("data");
    expect(record?.outcomes[2]?.kind).toBe("disconnect");
  });

  it("repeats on the interval rather than firing once", async () => {
    const record = golden.schedules.find((entry) => entry.name === "disconnect every third");
    expect(record?.outcomes.map((outcome) => outcome.kind)).toStrictEqual([
      "data",
      "data",
      "disconnect",
      "data",
      "data",
      "disconnect",
      "data",
    ]);
  });

  it("lets the disconnect win when both are due", async () => {
    // A read cannot both fail and return empty; the harsher fault is the
    // useful one to model.
    const record = golden.schedules.find((entry) => entry.name === "both, disconnect wins the tie");
    expect(record?.outcomes[1]?.kind).toBe("disconnect");
  });

  it("keeps the two schedules independent when they are out of phase", async () => {
    const record = golden.schedules.find((entry) => entry.name === "both, out of phase");
    expect(record?.outcomes.map((outcome) => outcome.kind)).toStrictEqual([
      "data",
      "timeout",
      "disconnect",
      "timeout",
      "data",
      "disconnect",
      "data",
    ]);
  });

  it("does nothing at all when nothing is configured", async () => {
    const record = golden.schedules.find((entry) => entry.name === "no faults");
    expect(record?.outcomes.every((outcome) => outcome.kind === "data")).toBe(true);
  });

  it("counts a read the fault schedule skipped", async () => {
    // The counter advances before the fault is decided, so an injected
    // disconnect still moves the schedule along.
    const record = golden.schedules.find((entry) => entry.name === "disconnect every one");
    expect(record?.outcomes.map((outcome) => outcome.message)).toStrictEqual(
      record?.outcomes.map((_outcome, index) => `chaos: injected disconnect on receive #${index + 1}`),
    );
  });
});

describe("the injected disconnect", () => {
  it("names the label so a log says which wrapper fired", async () => {
    const record = golden.schedules.find((entry) => entry.name === "a custom label");
    expect(record?.outcomes[1]?.message).toBe("flaky-bbs: injected disconnect on receive #2");
  });

  it("falls back to a label rather than an empty prefix", async () => {
    const record = golden.schedules.find((entry) => entry.name === "an empty label falls back");
    expect(record?.outcomes[1]?.message).toBe(`${golden.defaults.label}: injected disconnect on receive #2`);
  });

  it("takes the inner transport down with it", async () => {
    // A disconnect that leaves the link up is not the fault being modelled —
    // the caller would carry on reading as though nothing happened.
    const { sleep } = recorder();
    const inner = new FakeInner();
    const chaos = new ChaosTransport(inner, { disconnectEveryNReceives: 1, sleep });
    await chaos.connect("h", 1);
    await expect(chaos.receive(4096, 250)).rejects.toThrow(TransportConnectionError);
    expect(inner.isConnected()).toBe(false);
    expect(inner.calls.at(-1)).toStrictEqual(["disconnect"]);
  });

  it("still fails when the inner transport cannot be closed", async () => {
    // Cleanup that raises must not mask the fault the test asked for.
    const { sleep } = recorder();
    const inner = new FakeInner();
    inner.disconnect = async () => {
      throw new Error("already gone");
    };
    const chaos = new ChaosTransport(inner, { disconnectEveryNReceives: 1, sleep });
    await chaos.connect("h", 1);
    await expect(chaos.receive(4096, 250)).rejects.toThrow("injected disconnect on receive #1");
  });

  it("does not read from the inner transport", async () => {
    const { sleep } = recorder();
    const inner = new FakeInner();
    const chaos = new ChaosTransport(inner, { disconnectEveryNReceives: 1, sleep });
    await chaos.connect("h", 1);
    await expect(chaos.receive(4096, 250)).rejects.toThrow(TransportConnectionError);
    expect(inner.reads).toBe(0);
  });
});

describe("the injected timeout", () => {
  it("waits out the caller's own budget", async () => {
    // Returning empty instantly would let a test pass in a way a real dead
    // link never would.
    const { slept, sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { timeoutEveryNReceives: 1, sleep });
    await chaos.connect("h", 1);
    await chaos.receive(4096, 250);
    expect(slept).toStrictEqual(golden.jitter.timeout_delays_s);
  });

  it("does not read from the inner transport", async () => {
    const { sleep } = recorder();
    const inner = new FakeInner();
    const chaos = new ChaosTransport(inner, { timeoutEveryNReceives: 1, sleep });
    await chaos.connect("h", 1);
    expect((await chaos.receive(4096, 250)).length).toBe(0);
    expect(inner.reads).toBe(0);
  });

  it("never waits a negative time", async () => {
    // A caller may pass a nonsense budget; sleeping on it must not throw.
    const { slept, sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { timeoutEveryNReceives: 1, sleep });
    await chaos.connect("h", 1);
    await chaos.receive(4096, -100);
    expect(slept).toStrictEqual([0]);
  });
});

describe("jitter", () => {
  it("delays every read when a bound is set", async () => {
    const { slept, sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { maxJitterMs: 50, sleep });
    await chaos.connect("h", 1);
    for (let index = 0; index < golden.jitter.jittered_count; index += 1) {
      await chaos.receive(4096, 250);
    }
    expect(slept).toHaveLength(golden.jitter.jittered_count);
  });

  it("stays within the bound", async () => {
    const { slept, sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { maxJitterMs: 50, sleep });
    await chaos.connect("h", 1);
    for (let index = 0; index < 20; index += 1) {
      await chaos.receive(4096, 250);
    }
    expect(slept.every((value) => value >= 0 && value <= golden.jitter.jitter_bound_s)).toBe(
      golden.jitter.jitter_within_bound,
    );
  });

  it("does not always draw the same value", async () => {
    // A constant would make the jitter setting a no-op that still looks
    // configured.
    const { slept, sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { maxJitterMs: 50, sleep });
    await chaos.connect("h", 1);
    for (let index = 0; index < 10; index += 1) {
      await chaos.receive(4096, 250);
    }
    expect(new Set(slept).size).toBeGreaterThan(1);
  });

  it("does not sleep at all when there is no bound", async () => {
    const { slept, sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { sleep });
    await chaos.connect("h", 1);
    for (let index = 0; index < 4; index += 1) {
      await chaos.receive(4096, 250);
    }
    expect(slept).toHaveLength(golden.jitter.unjittered_count);
  });

  it("repeats exactly for a given seed", async () => {
    // Reproducibility is the point: a failure a test cannot re-run is not a
    // failure anyone can fix.
    const draws = async (seed: number) => {
      const { slept, sleep } = recorder();
      const chaos = new ChaosTransport(new FakeInner(), { maxJitterMs: 50, seed, sleep });
      await chaos.connect("h", 1);
      for (let index = 0; index < 6; index += 1) {
        await chaos.receive(4096, 250);
      }
      return slept;
    };
    expect(await draws(7)).toStrictEqual(await draws(7));
    expect(await draws(7)).not.toStrictEqual(await draws(8));
  });

  it("defaults to a fixed seed rather than a random one", async () => {
    const { slept, sleep } = recorder();
    const chaos = new ChaosTransport(new FakeInner(), { maxJitterMs: 50, sleep });
    await chaos.connect("h", 1);
    await chaos.receive(4096, 250);

    const seeded = recorder();
    const other = new ChaosTransport(new FakeInner(), {
      maxJitterMs: 50,
      seed: golden.defaults.seed,
      sleep: seeded.sleep,
    });
    await other.connect("h", 1);
    await other.receive(4096, 250);
    expect(slept).toStrictEqual(seeded.slept);
  });
});

describe("the default delay", () => {
  // Every other test injects a sleep. Without one case that takes the real
  // one, a wrapper that never actually waits would look fully covered.
  it("waits for real when injecting a timeout", async () => {
    const chaos = new ChaosTransport(new FakeInner(), { timeoutEveryNReceives: 1, maxJitterMs: 1 });
    await chaos.connect("h", 1);
    expect((await chaos.receive(4096, 1)).length).toBe(0);
  });
});

describe("everything else", () => {
  it("passes straight through to the inner transport", async () => {
    const inner = new FakeInner();
    const chaos = new ChaosTransport(inner);
    expect(chaos.isConnected()).toBe(golden.delegation.connected_before);
    await chaos.connect("bbs.example.org", 2323, { origin: "https://app.example.org" });
    expect(chaos.isConnected()).toBe(golden.delegation.connected_after_connect);
    await chaos.send(new TextEncoder().encode("ls\r"));
    await chaos.disconnect();
    expect(chaos.isConnected()).toBe(golden.delegation.connected_after_disconnect);
    expect(inner.calls).toStrictEqual(golden.delegation.calls);
  });

  it("reads liveness from the inner transport, not from a flag of its own", async () => {
    // The wrapper injects faults; it does not own the connection.
    const inner = new FakeInner();
    const chaos = new ChaosTransport(inner);
    await chaos.connect("h", 1);
    inner.connected = false;
    expect(chaos.isConnected()).toBe(false);
  });

  it("passes the caller's read budget down untouched", async () => {
    const { sleep } = recorder();
    const inner = new FakeInner();
    const chaos = new ChaosTransport(inner, { sleep });
    await chaos.connect("h", 1);
    await chaos.receive(1024, 75);
    expect(inner.calls.at(-1)).toStrictEqual(["receive", 1024, 75]);
  });
});
