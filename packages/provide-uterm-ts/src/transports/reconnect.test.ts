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
  classification: Array<{ name: string; error: string; retryable: boolean }>;
  exhausted_message: string;
  connect_exhausted_message: string;
  sequences: Array<{
    name: string;
    log: Array<Array<string | number>>;
    error: string | null;
    message: string | null;
  }>;
}

const golden = loadGolden<ReconnectGolden>("reconnect_golden.json");

/** The least a reconnecting session can wrap: a name and a close. */
function named(name: string): { name: string; close(): Promise<void> } {
  return { name, close: async () => {} };
}

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
        return named(`session-${connects}`);
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
    const proxy = reconnecting(async () => named("session"), { sleep });
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
        return named(`session-${connects}`);
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
        return named(`session-${connects}`);
      },
      { sleep },
    );
    const seen: string[] = [];
    await proxy.run(async (session) => {
      seen.push(session.name);
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
        return named("session");
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
    const proxy = reconnecting(async () => named("session"), { sleep, policy: { maxRetries: 2 } });
    await expect(proxy.run(flaky(99))).rejects.toThrow(golden.exhausted_message);
  });

  it("makes exactly one attempt when no retries are allowed", async () => {
    const { sleep } = recorder();
    let attempts = 0;
    const proxy = reconnecting(async () => named("session"), { sleep, policy: { maxRetries: 0 } });
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
    const proxy = reconnecting(async () => named("session"), { sleep, policy: { maxRetries: 0 } });
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
        return named(`session-${connects}`);
      },
      {
        sleep,
        onReconnect: async (session) => {
          seen.push(session.name);
        },
      },
    );
    await proxy.run(flaky(1));
    expect(seen).toStrictEqual(["session-2"]);
  });

  it("does not run the hook when nothing dropped", async () => {
    const { sleep } = recorder();
    let hooks = 0;
    const proxy = reconnecting(async () => named("session"), {
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
    const proxy = reconnecting(async () => named("session"), { sleep, policy: { baseBackoffS: 0 } });
    await proxy.run(flaky(1));
    expect(slept).toStrictEqual([]);
  });

  it("backs off between attempts otherwise", async () => {
    const { slept, sleep } = recorder();
    const proxy = reconnecting(async () => named("session"), { sleep });
    await proxy.run(flaky(2));
    expect(slept).toStrictEqual([0.5, 1]);
  });

  it("exposes the live session", async () => {
    const { sleep } = recorder();
    let connects = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return named(`session-${connects}`);
      },
      { sleep },
    );
    expect(proxy.session).toBeUndefined();
    await proxy.run(async () => "fine");
    expect(proxy.session?.name).toBe("session-1");
    await proxy.run(flaky(1));
    expect(proxy.session?.name).toBe("session-2");
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

/**
 * The same failure scripts the corpus drove the reference through, replayed
 * against the port — and compared on the *sequence*, not the answer. A retry
 * that reconnects before closing the dead socket returns the same value and
 * leaves a descriptor behind.
 */
describe("which failures are the transport", () => {
  /**
   * Each Python error as this runtime raises it. Node has no exception tree:
   * a syscall failure is an `Error` carrying a `code`, so the code is what
   * the port classifies on.
   */
  const AS_NODE: Record<string, Error> = {
    ConnectionError: Object.assign(new Error("dropped"), { name: "ConnectionError" }),
    BrokenPipeError: Object.assign(new Error("write EPIPE"), { code: "EPIPE" }),
    ConnectionResetError: Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" }),
    ConnectionRefusedError: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }),
    ConnectionAbortedError: Object.assign(new Error("ECONNABORTED"), { code: "ECONNABORTED" }),
    OSError: Object.assign(new Error("EHOSTUNREACH"), { code: "EHOSTUNREACH" }),
    TimeoutError: Object.assign(new Error("timed out"), { name: "TimeoutError" }),
    FileNotFoundError: Object.assign(new Error("open ENOENT"), { code: "ENOENT" }),
    PermissionError: Object.assign(new Error("EACCES"), { code: "EACCES" }),
    InterruptedError: Object.assign(new Error("EINTR"), { code: "EINTR" }),
    ValueError: new TypeError("bad value"),
    TypeError: new TypeError("bad type"),
    KeyError: new Error("missing"),
    RuntimeError: new Error("broken"),
    AssertionError: Object.assign(new Error("impossible"), { name: "AssertionError" }),
    Exception: new Error("something"),
  };

  /**
   * Where the port deliberately answers no and the reference yes.
   *
   * All three are `OSError` in Python, so the reference retries them for the
   * whole budget. None is a transport that might come back: a missing file, a
   * denied permission and an interrupted call are the same on the next
   * attempt, and retrying only delays the report.
   */
  const NARROWED = new Set(["FileNotFoundError", "PermissionError", "InterruptedError"]);

  it.each(golden.classification)("$name", (record) => {
    const error = AS_NODE[record.error] as Error;
    const expected = NARROWED.has(record.error) ? false : record.retryable;
    expect(isRetryableTransportError(error)).toBe(expected);
  });

  it("retries a timeout, which is the transport and not the caller", () => {
    // However it arrives: a code from a socket, or the name `AbortSignal`
    // gives a timed-out fetch.
    expect(isRetryableTransportError(Object.assign(new Error("x"), { code: "ETIMEDOUT" }))).toBe(true);
    expect(isRetryableTransportError(Object.assign(new Error("x"), { name: "TimeoutError" }))).toBe(true);
  });

  it("retries what this runtime calls a drop, whatever Python calls it", () => {
    // The corpus cannot reach these: `AbortError` is what an aborted socket
    // raises here and has no Python counterpart, and `ConnectionClosed` is
    // the websocket library's — the mirror of the reference's own
    // `websockets.ConnectionClosed`.
    for (const name of ["AbortError", "ConnectionClosed"]) {
      expect(isRetryableTransportError(Object.assign(new Error("gone"), { name }))).toBe(true);
    }
  });

  it("does not retry what only the caller can fix", () => {
    for (const error of [new TypeError("bad"), new RangeError("bad"), new Error("plain"), new SyntaxError("bad")]) {
      expect(isRetryableTransportError(error)).toBe(false);
    }
  });

  it("does not retry something that is not an error at all", () => {
    for (const value of [undefined, null, "ECONNRESET", 42, { code: "ECONNRESET" }]) {
      expect(isRetryableTransportError(value)).toBe(false);
    }
  });
});

describe("telling the two exhaustions apart", () => {
  /** The exact message, since one of the two contains the other. */
  async function messageOf(run: () => Promise<unknown>): Promise<string> {
    try {
      await run();
    } catch (error) {
      return (error as Error).message;
    }
    throw new Error("expected a failure");
  }

  it("says a server never came up, or could not be got back", async () => {
    // `toThrow("connect retries exhausted")` matches both — the first message
    // is a substring of the second — so these compare exactly.
    const dead = async (): Promise<{ close(): Promise<void> }> => {
      throw Object.assign(new Error("ECONNREFUSED"), { name: "ConnectionError" });
    };
    const sleep = async (): Promise<void> => {};
    expect(await messageOf(() => connectWithRetries(dead, { sleep, policy: { maxRetries: 1 } }))).toBe(
      golden.connect_exhausted_message,
    );
    expect(
      await messageOf(() => reconnecting(dead, { sleep, policy: { maxRetries: 1 } }).run(async () => "never")),
    ).toBe(golden.connect_exhausted_message);

    // Up once, then gone: the rebuild reports getting back, not getting up.
    let connects = 0;
    const flakyConnect = async (): Promise<{ close(): Promise<void> }> => {
      connects += 1;
      if (connects > 1) {
        throw Object.assign(new Error("ECONNREFUSED"), { name: "ConnectionError" });
      }
      return { close: async () => {} };
    };
    expect(
      await messageOf(() =>
        reconnecting(flakyConnect, { sleep, policy: { maxRetries: 1 } }).run(async () => {
          throw Object.assign(new Error("ECONNRESET"), { name: "ConnectionError" });
        }),
      ),
    ).toBe(golden.exhausted_message);
  });

  it("holds no session once it has given up", async () => {
    // A caller reading `session` after the failure would otherwise get the
    // one that just died.
    const proxy = reconnecting(async () => named("session"), { sleep: async () => {}, policy: { maxRetries: 0 } });
    await expect(
      proxy.run(async () => {
        throw Object.assign(new Error("ECONNRESET"), { name: "ConnectionError" });
      }),
    ).rejects.toThrow();
    expect(proxy.session).toBeUndefined();
  });

  it("holds no session when the rebuild itself cannot connect", async () => {
    // The other way out: the drop was retryable, the old session was closed,
    // and the server never came back. Nothing may still name what was closed.
    let connects = 0;
    const proxy = reconnecting(
      async () => {
        connects += 1;
        if (connects > 1) {
          throw Object.assign(new Error("ECONNREFUSED"), { name: "ConnectionError" });
        }
        return named("session-1");
      },
      { sleep: async () => {}, policy: { maxRetries: 1 } },
    );
    await expect(
      proxy.run(async () => {
        throw Object.assign(new Error("ECONNRESET"), { name: "ConnectionError" });
      }),
    ).rejects.toThrow(golden.exhausted_message);
    expect(proxy.session).toBeUndefined();
  });

  it("has the new session live before the hook runs", async () => {
    // The hook is where application state is rebuilt, and it may well ask the
    // wrapper for the session it is rebuilding on.
    let seen: string | undefined = "unset";
    let connects = 0;
    const proxy = reconnecting(async () => named(`session-${(connects += 1)}`), {
      sleep: async () => {},
      onReconnect: async () => {
        seen = proxy.session?.name;
      },
    });
    let calls = 0;
    await proxy.run(async () => {
      calls += 1;
      if (calls < 2) {
        throw Object.assign(new Error("ECONNRESET"), { name: "ConnectionError" });
      }
      return "ok";
    });
    expect(seen).toBe("session-2");
  });
});

describe("what a reconnecting session actually does, in order", () => {
  /** Builds a session that fails on cue and writes down what it was asked. */
  function harness(record: ReconnectGolden["sequences"][number], script: SequenceScript) {
    const log: Array<Array<string | number>> = [];
    let attempts = 0;

    const connect = async (): Promise<RecordingSession> => {
      const index = attempts;
      attempts += 1;
      const failure = script.connectFailures[index] ?? "ok";
      log.push(["connect", failure]);
      if (failure !== "ok") {
        throw Object.assign(new Error(failure), { name: "ConnectionError" });
      }
      return new RecordingSession(log, `session-${index}`, script.sendFailures[String(index)] ?? [], script.closeFails);
    };

    return { log, connect, record };
  }

  interface SequenceScript {
    connectFailures: string[];
    sendFailures: Record<string, string[]>;
    closeFails: boolean;
    hook: boolean;
    policy: { maxRetries: number; baseBackoffS: number; maxBackoffS: number };
  }

  /** A session recording what was asked of it, failing on cue. */
  class RecordingSession {
    private sends = 0;
    readonly log: Array<Array<string | number>>;
    readonly name: string;
    readonly failures: string[];
    readonly closeFails: boolean;

    constructor(log: Array<Array<string | number>>, name: string, failures: string[], closeFails: boolean) {
      this.log = log;
      this.name = name;
      this.failures = failures;
      this.closeFails = closeFails;
    }

    async close(): Promise<void> {
      if (this.closeFails) {
        this.log.push(["close", this.name, "raised"]);
        throw Object.assign(new Error("already gone"), { name: "ConnectionError" });
      }
      this.log.push(["close", this.name]);
    }

    async send(): Promise<void> {
      const failure = this.failures[this.sends] ?? "ok";
      this.sends += 1;
      this.log.push(["send", this.name, failure]);
      if (failure === "connection" || failure === "os") {
        throw Object.assign(new Error("socket went away"), { name: "ConnectionError" });
      }
      if (failure === "value") {
        throw new TypeError("the caller's own bug");
      }
    }
  }

  const SCRIPTS: Record<string, SequenceScript> = {
    "a send that works": {
      connectFailures: [],
      sendFailures: {},
      closeFails: false,
      hook: true,
      policy: { maxRetries: 5, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a socket that drops once": {
      connectFailures: [],
      sendFailures: { "0": ["connection"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 5, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a socket that drops on an OSError": {
      connectFailures: [],
      sendFailures: { "0": ["os"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 5, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "the caller's own bug": {
      connectFailures: [],
      sendFailures: { "0": ["value"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 5, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a socket that drops twice": {
      connectFailures: [],
      sendFailures: { "0": ["connection"], "1": ["connection"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 5, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a session that never stays up": {
      connectFailures: [],
      sendFailures: Object.fromEntries([0, 1, 2, 3, 4, 5].map((n) => [String(n), ["connection"]])),
      closeFails: false,
      hook: true,
      policy: { maxRetries: 2, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a server that is hard down": {
      connectFailures: Array(6).fill("refused"),
      sendFailures: {},
      closeFails: false,
      hook: true,
      policy: { maxRetries: 2, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a server down for the first attempt only": {
      connectFailures: ["refused"],
      sendFailures: {},
      closeFails: false,
      hook: true,
      policy: { maxRetries: 2, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a drop, then a server that will not come back": {
      connectFailures: ["ok", "refused", "refused", "refused"],
      sendFailures: { "0": ["connection"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 1, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "no retries allowed at all": {
      connectFailures: [],
      sendFailures: { "0": ["connection"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 0, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "no backoff configured": {
      connectFailures: [],
      sendFailures: { "0": ["connection"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 5, baseBackoffS: 0, maxBackoffS: 30 },
    },
    "a ceiling below the base": {
      connectFailures: [],
      sendFailures: { "0": ["connection"], "1": ["connection"], "2": ["connection"] },
      closeFails: false,
      hook: true,
      policy: { maxRetries: 3, baseBackoffS: 4, maxBackoffS: 1 },
    },
    "a session whose close fails on the way out": {
      connectFailures: [],
      sendFailures: { "0": ["connection"] },
      closeFails: true,
      hook: true,
      policy: { maxRetries: 5, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
    "a drop with nobody watching for the reconnect": {
      connectFailures: [],
      sendFailures: { "0": ["connection"] },
      closeFails: false,
      hook: false,
      policy: { maxRetries: 5, baseBackoffS: 0.5, maxBackoffS: 30 },
    },
  };

  it.each(golden.sequences)("$name", async (record) => {
    const script = SCRIPTS[record.name] as SequenceScript;
    const { log, connect } = harness(record, script);
    const session = reconnecting(connect, {
      policy: script.policy,
      sleep: async (seconds) => {
        log.push(["sleep", seconds]);
      },
      ...(script.hook
        ? {
            onReconnect: async (fresh: RecordingSession) => {
              log.push(["hook", (fresh as unknown as { name: string }).name]);
            },
          }
        : {}),
    });

    let error: string | null = null;
    let message: string | null = null;
    try {
      await session.run((live) => live.send());
    } catch (thrown) {
      error = thrown instanceof TypeError ? "ValueError" : "ConnectionError";
      message = (thrown as Error).message;
    }
    expect(log).toEqual(record.log);
    expect(error).toBe(record.error);
    if (record.message !== null) {
      expect(message).toBe(record.message);
    }
  });
});
