//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  connectWithRetries,
  isRetryableTransportError,
  policyDelay,
  RECONNECT_DEFAULTS,
  reconnecting,
} from "./index.ts";

interface ReconnectGolden {
  defaults: { max_retries: number; base_backoff_s: number; max_backoff_s: number };
  schedules: Array<{
    name: string;
    max_retries: number;
    base_backoff_s: number;
    max_backoff_s: number;
    delays: number[];
  }>;
  exhausted_message: string;
  connect_exhausted_message: string;
}

const golden = loadGolden<ReconnectGolden>("reconnect_golden.json");

/** Records what it was asked to sleep for, without sleeping. */
function recorder() {
  const slept: number[] = [];
  return { slept, sleep: async (seconds: number) => void slept.push(seconds) };
}

describe("policyDelay", () => {
  it.each(golden.schedules)("$name", (record) => {
    // Exponential from a one-based attempt number and bounded, so a long
    // outage settles at a steady rate rather than drifting towards never
    // retrying.
    const policy = { baseBackoffS: record.base_backoff_s, maxBackoffS: record.max_backoff_s };
    expect(record.delays.map((_delay, attempt) => policyDelay(policy, attempt))).toStrictEqual(record.delays);
  });

  it("treats the first attempt as the base delay", () => {
    // One-based, so attempt one is the base rather than double it. Off by
    // one here doubles every wait in the schedule.
    const record = golden.schedules.find((entry) => entry.name === "defaults");
    expect(record?.delays[1]).toBe(record?.base_backoff_s);
    expect(record?.delays[2]).toBe((record?.base_backoff_s ?? 0) * 2);
  });

  it("clamps an attempt of zero rather than halving the base", () => {
    const record = golden.schedules.find((entry) => entry.name === "defaults");
    expect(record?.delays[0]).toBe(record?.base_backoff_s);
  });

  it("saturates at the ceiling", () => {
    const record = golden.schedules.find((entry) => entry.name === "defaults");
    expect(record?.delays.at(-1)).toBe(record?.max_backoff_s);
  });

  it("matches the reference defaults", () => {
    expect(RECONNECT_DEFAULTS.maxRetries).toBe(golden.defaults.max_retries);
    expect(RECONNECT_DEFAULTS.baseBackoffS).toBe(golden.defaults.base_backoff_s);
    expect(RECONNECT_DEFAULTS.maxBackoffS).toBe(golden.defaults.max_backoff_s);
  });
});

describe("isRetryableTransportError", () => {
  it("retries a connection fault", () => {
    for (const error of [
      new Error("ECONNRESET"),
      Object.assign(new Error("closed"), { code: "ECONNRESET" }),
      Object.assign(new Error("gone"), { name: "ConnectionClosed" }),
      Object.assign(new Error("refused"), { code: "ECONNREFUSED" }),
      Object.assign(new Error("broken"), { code: "EPIPE" }),
    ]) {
      expect(isRetryableTransportError(error)).toBe(true);
    }
  });

  it("does not retry a programming error", () => {
    // Retrying one just delays the report by the whole budget, and the
    // second attempt fails exactly as the first did.
    for (const error of [new TypeError("bad"), new RangeError("bad"), new SyntaxError("bad")]) {
      expect(isRetryableTransportError(error)).toBe(false);
    }
  });

  it("does not retry something that is not an error at all", () => {
    for (const value of [undefined, null, "boom", 42, {}]) {
      expect(isRetryableTransportError(value)).toBe(false);
    }
  });
});

describe("the default backoff", () => {
  // Every other test injects a sleep. Without one case that takes the real
  // one, a wrapper that never actually waits — or one that waits forever —
  // would look fully covered.
  const brief = { maxRetries: 1, baseBackoffS: 0.001, maxBackoffS: 0.001 };

  it("waits for real between connect attempts", async () => {
    let attempts = 0;
    const session = await connectWithRetries(
      async () => {
        attempts += 1;
        if (attempts < 2) {
          throw new Error("ECONNREFUSED");
        }
        return "session";
      },
      { policy: brief },
    );
    expect(session).toBe("session");
    expect(attempts).toBe(2);
  });

  it("waits for real between reconnect attempts", async () => {
    let connects = 0;
    let calls = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return `session-${connects}`;
      },
      { policy: brief },
    );
    const result = await proxy.run(async () => {
      calls += 1;
      if (calls < 2) {
        throw new Error("ECONNRESET");
      }
      return "ok";
    });
    expect(result).toBe("ok");
    expect(connects).toBe(2);
  });
});

describe("connectWithRetries", () => {
  it("returns the first successful connection", async () => {
    const { slept } = recorder();
    expect(await connectWithRetries(async () => "session")).toBe("session");
    expect(slept).toStrictEqual([]);
  });

  it("retries until it succeeds", async () => {
    const { slept, sleep } = recorder();
    let attempts = 0;
    const session = await connectWithRetries(
      async () => {
        attempts += 1;
        if (attempts < 3) {
          throw new Error("ECONNREFUSED");
        }
        return "session";
      },
      { sleep },
    );
    expect(session).toBe("session");
    expect(slept).toStrictEqual([0.5, 1]);
  });

  it("gives up once the budget is spent", async () => {
    // The budget is what stops a client hammering a server that is down.
    const { slept, sleep } = recorder();
    await expect(
      connectWithRetries(
        async () => {
          throw new Error("ECONNREFUSED");
        },
        { sleep, policy: { maxRetries: 2 } },
      ),
    ).rejects.toThrow(golden.connect_exhausted_message);
    expect(slept).toHaveLength(2);
  });

  it("makes exactly one attempt when no retries are allowed", async () => {
    const { sleep } = recorder();
    let attempts = 0;
    await expect(
      connectWithRetries(
        async () => {
          attempts += 1;
          throw new Error("ECONNREFUSED");
        },
        { sleep, policy: { maxRetries: 0 } },
      ),
    ).rejects.toThrow(golden.connect_exhausted_message);
    expect(attempts).toBe(1);
  });

  it("skips the backoff entirely when it is zero", async () => {
    // A caller asking for no backoff should not be charged a turn of the
    // event loop per attempt.
    const { slept, sleep } = recorder();
    let attempts = 0;
    await connectWithRetries(
      async () => {
        attempts += 1;
        if (attempts < 2) {
          throw new Error("ECONNREFUSED");
        }
        return "session";
      },
      { sleep, policy: { baseBackoffS: 0 } },
    );
    expect(slept).toStrictEqual([]);
  });

  it("keeps the cause so the real failure is not lost", async () => {
    // "retries exhausted" alone tells an operator nothing about why.
    const { sleep } = recorder();
    const original = new Error("ECONNREFUSED");
    await expect(
      connectWithRetries(
        async () => {
          throw original;
        },
        { sleep, policy: { maxRetries: 0 } },
      ),
    ).rejects.toMatchObject({ cause: original });
  });
});

describe("reconnecting", () => {
  /** A session that fails a given number of times, then works. */
  function flaky(failures: number, error: () => unknown = () => new Error("ECONNRESET")) {
    let remaining = failures;
    return async () => {
      if (remaining > 0) {
        remaining -= 1;
        throw error();
      }
      return "ok";
    };
  }

  it("passes a working call straight through", async () => {
    const { slept, sleep } = recorder();
    const proxy = reconnecting(async () => "session", { sleep });
    expect(await proxy.run(async () => "result")).toBe("result");
    expect(slept).toStrictEqual([]);
  });

  it("reconnects and retries after a transport drop", async () => {
    // The point of the wrapper: a drop mid-session should come back without
    // the caller noticing.
    const { sleep } = recorder();
    let connects = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return `session-${connects}`;
      },
      { sleep },
    );
    expect(await proxy.run(flaky(1))).toBe("ok");
    expect(connects).toBe(2);
  });

  it("hands each retry the freshly connected session", async () => {
    // Retrying against the dead session would fail the same way forever.
    const { sleep } = recorder();
    let connects = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return `session-${connects}`;
      },
      { sleep },
    );
    const seen: string[] = [];
    await proxy.run(async (session) => {
      seen.push(session);
      if (seen.length < 2) {
        throw new Error("ECONNRESET");
      }
      return "ok";
    });
    expect(seen).toStrictEqual(["session-1", "session-2"]);
  });

  it("does not reconnect for an error that is not a transport fault", async () => {
    const { sleep } = recorder();
    let connects = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return "session";
      },
      { sleep },
    );
    await expect(
      proxy.run(async () => {
        throw new TypeError("bad call");
      }),
    ).rejects.toThrow(TypeError);
    expect(connects).toBe(1);
  });

  it("gives up once the budget is spent", async () => {
    const { sleep } = recorder();
    const proxy = reconnecting(async () => "session", { sleep, policy: { maxRetries: 2 } });
    await expect(proxy.run(flaky(99))).rejects.toThrow(golden.exhausted_message);
  });

  it("makes exactly one attempt when no retries are allowed", async () => {
    const { sleep } = recorder();
    let attempts = 0;
    const proxy = reconnecting(async () => "session", { sleep, policy: { maxRetries: 0 } });
    await expect(
      proxy.run(async () => {
        attempts += 1;
        throw new Error("ECONNRESET");
      }),
    ).rejects.toThrow(golden.exhausted_message);
    expect(attempts).toBe(1);
  });

  it("keeps the cause when it gives up", async () => {
    // "retries exhausted" alone tells an operator nothing about what broke.
    const { sleep } = recorder();
    const original = new Error("ECONNRESET");
    const proxy = reconnecting(async () => "session", { sleep, policy: { maxRetries: 0 } });
    await expect(
      proxy.run(async () => {
        throw original;
      }),
    ).rejects.toMatchObject({ cause: original });
  });

  it("runs the reconnect hook with the new session", async () => {
    // Application state — a login, a subscription — has to be re-established
    // before the retried call runs, or it fails for a second reason.
    const { sleep } = recorder();
    const seen: string[] = [];
    let connects = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return `session-${connects}`;
      },
      {
        sleep,
        onReconnect: async (session) => {
          seen.push(session);
        },
      },
    );
    await proxy.run(flaky(1));
    expect(seen).toStrictEqual(["session-2"]);
  });

  it("does not run the hook when nothing dropped", async () => {
    const { sleep } = recorder();
    let hooks = 0;
    const proxy = reconnecting(async () => "session", {
      sleep,
      onReconnect: async () => {
        hooks += 1;
      },
    });
    await proxy.run(async () => "fine");
    expect(hooks).toBe(0);
  });

  it("skips the backoff entirely when it is zero", async () => {
    // A caller asking for no backoff should not be charged a turn of the
    // event loop per attempt.
    const { slept, sleep } = recorder();
    const proxy = reconnecting(async () => "session", { sleep, policy: { baseBackoffS: 0 } });
    await proxy.run(flaky(1));
    expect(slept).toStrictEqual([]);
  });

  it("backs off between attempts otherwise", async () => {
    const { slept, sleep } = recorder();
    const proxy = reconnecting(async () => "session", { sleep });
    await proxy.run(flaky(2));
    expect(slept).toStrictEqual([0.5, 1]);
  });

  it("exposes the live session", async () => {
    const { sleep } = recorder();
    let connects = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return `session-${connects}`;
      },
      { sleep },
    );
    expect(proxy.session).toBeUndefined();
    await proxy.run(async () => "fine");
    expect(proxy.session).toBe("session-1");
    await proxy.run(flaky(1));
    expect(proxy.session).toBe("session-2");
  });

  it("gives up when reconnecting itself keeps failing", async () => {
    const { sleep } = recorder();
    const proxy = reconnecting(
      async () => {
        throw new Error("ECONNREFUSED");
      },
      { sleep, policy: { maxRetries: 1 } },
    );
    await expect(proxy.run(async () => "never")).rejects.toThrow(golden.connect_exhausted_message);
  });
});
