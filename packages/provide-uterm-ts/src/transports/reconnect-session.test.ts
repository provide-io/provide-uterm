//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { golden, named } from "../testing/reconnect-harness.ts";
import { connectWithRetries, isRetryableTransportError, reconnecting } from "./index.ts";

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
    const proxy = reconnecting(
      async () => {
        connects += 1;
        return named(`session-${connects}`);
      },
      {
        sleep: async () => {},
        onReconnect: async () => {
          seen = proxy.session?.name;
        },
      },
    );
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
