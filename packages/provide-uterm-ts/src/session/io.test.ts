//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  DEFAULT_INPUT_TYPE,
  DEFAULT_PROMPT_IDLE_GRACE_RATIO,
  DEFAULT_PROMPT_READ_INTERVAL_MS,
  DEFAULT_PROMPT_REQUIRE_IDLE,
  DEFAULT_PROMPT_TIMEOUT_MS,
  DEFAULT_WAIT_AFTER_SEC,
  type PromptSession,
  PromptWaiter,
  sendInput,
} from "./index.ts";

interface IoGolden {
  defaults: {
    prompt_timeout_ms: number;
    prompt_read_interval_ms: number;
    prompt_require_idle: boolean;
    prompt_idle_grace_ratio: number;
    input_type: string;
    wait_after_sec: number;
  };
  sends: Array<{ name: string; keys: string; input_type: string | null; sent: string[] }>;
}

const golden = loadGolden<IoGolden>("io_golden.json");

/** A session whose snapshots the test scripts. */
class ScriptedSession implements PromptSession {
  readonly sent: string[] = [];
  readonly waits: number[] = [];
  connected = true;
  /** Seconds until the screen is expected to settle, when the session knows. */
  secondsUntilIdle: (() => number) | undefined;
  #index = 0;
  readonly #snapshots: Array<Record<string, unknown>>;

  constructor(snapshots: Array<Record<string, unknown>>) {
    this.#snapshots = snapshots;
  }

  isConnected(): boolean {
    return this.connected;
  }

  snapshot(): Record<string, unknown> {
    return this.#snapshots[Math.min(this.#index, this.#snapshots.length - 1)] ?? {};
  }

  async send(data: string): Promise<void> {
    this.sent.push(data);
  }

  async waitForUpdate(options: { timeoutMs: number; since?: number }): Promise<boolean> {
    this.waits.push(options.timeoutMs);
    this.#index += 1;
    return true;
  }
}

/** A prompt-detection payload. */
function prompt(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { screen: "hello", prompt_detected: { prompt_id: "main_menu", is_idle: true, ...overrides } };
}

describe("io defaults", () => {
  it("match the reference", () => {
    // These are the timings an automation inherits when it asks for nothing
    // in particular, so they are part of the contract rather than tuning.
    expect(DEFAULT_PROMPT_TIMEOUT_MS).toBe(golden.defaults.prompt_timeout_ms);
    expect(DEFAULT_PROMPT_READ_INTERVAL_MS).toBe(golden.defaults.prompt_read_interval_ms);
    expect(DEFAULT_PROMPT_REQUIRE_IDLE).toBe(golden.defaults.prompt_require_idle);
    expect(DEFAULT_PROMPT_IDLE_GRACE_RATIO).toBe(golden.defaults.prompt_idle_grace_ratio);
    expect(DEFAULT_INPUT_TYPE).toBe(golden.defaults.input_type);
    expect(DEFAULT_WAIT_AFTER_SEC).toBe(golden.defaults.wait_after_sec);
  });
});

describe("sendInput", () => {
  it.each(golden.sends)("$name", async (record) => {
    const session = new ScriptedSession([{}]);
    await sendInput(session, record.keys, {
      ...(record.input_type === null ? {} : { inputType: record.input_type }),
      waitAfterSec: 0,
    });
    expect(session.sent).toStrictEqual(record.sent);
  });

  it("sends a single key bare", async () => {
    // Appending a return to a menu that wanted one keypress submits an extra
    // blank line, which on most menus means "repeat the last thing".
    const record = golden.sends.find((entry) => entry.name === "single key");
    expect(record?.sent).toStrictEqual(["y"]);
  });

  it("sends a space for any-key and ignores what it was given", async () => {
    // "Press any key" wants a keypress, not the caller's text.
    const record = golden.sends.find((entry) => entry.name === "any key");
    expect(record?.sent).toStrictEqual([" "]);
  });

  it("falls back to multi-key for an unrecognised type", async () => {
    // A typo in the prompt type should still submit rather than hang waiting
    // for a return that never comes.
    expect(golden.sends.find((entry) => entry.name === "unknown type falls back to multi key")?.sent).toStrictEqual([
      "hello\r",
    ]);
  });

  it("does not deduplicate a return the caller already sent", async () => {
    // Faithful to the reference: it appends unconditionally rather than
    // guessing whether the caller meant two submissions.
    expect(golden.sends.find((entry) => entry.name === "keys already ending in a return")?.sent).toStrictEqual([
      "hello\r\r",
    ]);
  });

  it("waits after sending when asked", async () => {
    const session = new ScriptedSession([{}]);
    const started = Date.now();
    await sendInput(session, "x", { inputType: "single_key", waitAfterSec: 0.05 });
    expect(Date.now() - started).toBeGreaterThanOrEqual(40);
  });

  it("does not wait at all when the delay is zero or negative", async () => {
    // Not "waits briefly" — a pause of nothing must not be scheduled, since
    // even a zero-length one costs the caller a turn of the event loop.
    const session = new ScriptedSession([{}]);
    const slept: number[] = [];
    const sleep = async (seconds: number) => {
      slept.push(seconds);
    };
    await sendInput(session, "x", { waitAfterSec: 0, sleep });
    await sendInput(session, "x", { waitAfterSec: -1, sleep });
    expect(slept).toStrictEqual([]);
    await sendInput(session, "x", { waitAfterSec: 0.25, sleep });
    expect(slept).toStrictEqual([0.25]);
  });

  it("uses the default pause when the caller gives none", async () => {
    const session = new ScriptedSession([{}]);
    const slept: number[] = [];
    await sendInput(session, "x", {
      sleep: async (seconds) => {
        slept.push(seconds);
      },
    });
    expect(slept).toStrictEqual([DEFAULT_WAIT_AFTER_SEC]);
  });

  it("reads a plain connection flag as well as a method", async () => {
    // Some transports expose it as a property rather than a call.
    const up = {
      isConnected: true,
      sent: [] as string[],
      async send(data: string) {
        this.sent.push(data);
      },
    };
    await sendInput(up as unknown as PromptSession, "x", { waitAfterSec: 0 });
    expect(up.sent).toStrictEqual(["x\r"]);

    const down = { isConnected: false, async send() {} };
    await expect(sendInput(down as unknown as PromptSession, "x")).rejects.toThrow("Session disconnected");
  });

  it("refuses a session that is not there", async () => {
    await expect(sendInput(undefined, "x")).rejects.toThrow("Session is None");
  });

  it("refuses a disconnected session", async () => {
    // Better a clear error than keystrokes silently dropped into a closed
    // socket.
    const session = new ScriptedSession([{}]);
    session.connected = false;
    await expect(sendInput(session, "x")).rejects.toThrow("Session disconnected");
  });

  it("accepts a session that cannot say whether it is connected", async () => {
    // Absence of the check is not evidence of disconnection.
    const session = {
      sent: [] as string[],
      async send(data: string) {
        this.sent.push(data);
      },
    };
    await sendInput(session as unknown as PromptSession, "x", { waitAfterSec: 0 });
    expect(session.sent).toStrictEqual(["x\r"]);
  });
});

describe("PromptWaiter", () => {
  it("returns the detected prompt", async () => {
    const session = new ScriptedSession([prompt()]);
    const waiter = new PromptWaiter(session);
    expect(await waiter.waitForPrompt()).toStrictEqual({
      screen: "hello",
      promptId: "main_menu",
      inputType: undefined,
      kvData: undefined,
      isIdle: true,
    });
  });

  it("carries the input type and any parsed fields through", async () => {
    const session = new ScriptedSession([
      { screen: "s", prompt_detected: { prompt_id: "p", is_idle: true, input_type: "single_key", kv_data: { a: 1 } } },
    ]);
    const result = await new PromptWaiter(session).waitForPrompt();
    expect(result.inputType).toBe("single_key");
    expect(result.kvData).toStrictEqual({ a: 1 });
  });

  it("reports the screen on every poll", async () => {
    // The callback is how a caller renders progress while waiting.
    const seen: string[] = [];
    const session = new ScriptedSession([{ screen: "one" }, prompt()]);
    await new PromptWaiter(session, (screen) => seen.push(screen)).waitForPrompt();
    expect(seen).toStrictEqual(["one", "hello"]);
  });

  it("keeps polling until a prompt appears", async () => {
    const session = new ScriptedSession([{ screen: "booting" }, { screen: "still" }, prompt()]);
    expect((await new PromptWaiter(session).waitForPrompt()).promptId).toBe("main_menu");
    expect(session.waits).toHaveLength(2);
  });

  it("gives up after the timeout", async () => {
    const session = new ScriptedSession([{ screen: "booting" }]);
    await expect(new PromptWaiter(session).waitForPrompt({ timeoutMs: 5 })).rejects.toThrow(/No prompt detected/);
  });

  it("refuses a session that is not there", async () => {
    await expect(new PromptWaiter(undefined).waitForPrompt()).rejects.toThrow("Session is None");
  });

  it("refuses a session that has disconnected", async () => {
    const session = new ScriptedSession([prompt()]);
    session.connected = false;
    await expect(new PromptWaiter(session).waitForPrompt()).rejects.toThrow("Session disconnected");
  });

  it("skips a prompt whose id does not match", async () => {
    const session = new ScriptedSession([prompt({ prompt_id: "login" }), prompt({ prompt_id: "main_menu" })]);
    const result = await new PromptWaiter(session).waitForPrompt({ expectedPromptId: "main" });
    expect(result.promptId).toBe("main_menu");
  });

  it("matches an expected id by substring", async () => {
    // So a caller can wait for "menu" without knowing the full identifier.
    const session = new ScriptedSession([prompt({ prompt_id: "main_menu" })]);
    expect((await new PromptWaiter(session).waitForPrompt({ expectedPromptId: "menu" })).promptId).toBe("main_menu");
  });

  it("skips a prompt the caller's filter rejects", async () => {
    const session = new ScriptedSession([prompt({ prompt_id: "a" }), prompt({ prompt_id: "b" })]);
    const result = await new PromptWaiter(session).waitForPrompt({
      onPromptDetected: (detected) => detected.prompt_id === "b",
    });
    expect(result.promptId).toBe("b");
  });

  it("tells the caller why each candidate was rejected", async () => {
    // Without a reason a caller watching a wait fail cannot tell a filter
    // from a mismatch from an unsettled screen.
    const reasons: string[] = [];
    const session = new ScriptedSession([
      prompt({ prompt_id: "login" }),
      prompt({ prompt_id: "main_menu", is_idle: false }),
      prompt({ prompt_id: "main_menu" }),
    ]);
    await new PromptWaiter(session).waitForPrompt({
      expectedPromptId: "main",
      onPromptRejected: (_detected, reason) => reasons.push(reason),
    });
    expect(reasons).toStrictEqual(["expected_mismatch", "not_idle"]);
  });

  it("fires the seen callback for every candidate", async () => {
    const seen: string[] = [];
    const session = new ScriptedSession([prompt({ prompt_id: "a" }), prompt({ prompt_id: "b" })]);
    await new PromptWaiter(session).waitForPrompt({
      expectedPromptId: "b",
      onPromptSeen: (detected) => seen.push(String(detected.prompt_id)),
    });
    expect(seen).toStrictEqual(["a", "b"]);
  });

  it("attaches the screen and its hash to every candidate", async () => {
    const seen: Array<Record<string, unknown>> = [];
    const session = new ScriptedSession([
      { screen: "hello", screen_hash: "abc", captured_at: 5, prompt_detected: { prompt_id: "p", is_idle: true } },
    ]);
    await new PromptWaiter(session).waitForPrompt({ onPromptSeen: (detected) => seen.push(detected) });
    expect(seen[0]).toMatchObject({ prompt_id: "p", screen: "hello", screen_hash: "abc", captured_at: 5 });
  });

  it("waits for an unsettled screen before accepting a prompt", async () => {
    // A prompt drawn mid-repaint can be the wrong one; waiting for idle is
    // what stops an automation answering a menu that is still rendering.
    const session = new ScriptedSession([prompt({ is_idle: false }), prompt({ is_idle: true })]);
    expect((await new PromptWaiter(session).waitForPrompt()).isIdle).toBe(true);
    expect(session.waits).toHaveLength(1);
  });

  it("accepts an unsettled prompt once the grace period has passed", async () => {
    // Better a prompt that may still be repainting than no answer at all.
    const session = new ScriptedSession([prompt({ is_idle: false })]);
    const result = await new PromptWaiter(session).waitForPrompt({ timeoutMs: 50, idleGraceRatio: 0 });
    expect(result.isIdle).toBe(false);
  });

  it("accepts an unsettled prompt when idleness is not required", async () => {
    const session = new ScriptedSession([prompt({ is_idle: false })]);
    expect((await new PromptWaiter(session).waitForPrompt({ requireIdle: false })).isIdle).toBe(false);
  });

  it("asks the session how long until it settles", async () => {
    // The session usually knows better than a fixed interval does.
    const session = new ScriptedSession([prompt({ is_idle: false }), prompt()]);
    session.secondsUntilIdle = () => 0.01;
    await new PromptWaiter(session).waitForPrompt({ timeoutMs: 5000 });
    expect(session.waits[0]).toBe(10);
  });

  it("falls back to the read interval when the session does not know", async () => {
    const session = new ScriptedSession([prompt({ is_idle: false }), prompt()]);
    await new PromptWaiter(session).waitForPrompt({ timeoutMs: 5000, readIntervalMs: 30 });
    expect(session.waits[0]).toBe(30);
  });

  it("never waits past its own deadline", async () => {
    const session = new ScriptedSession([{ screen: "booting" }]);
    await expect(new PromptWaiter(session).waitForPrompt({ timeoutMs: 20, readIntervalMs: 10_000 })).rejects.toThrow();
    for (const wait of session.waits) {
      expect(wait).toBeLessThanOrEqual(20);
    }
  });

  it("copes with a session that cannot take a snapshot", async () => {
    // A transport may not expose one at all; the wait should time out
    // cleanly rather than throw on the first poll.
    const session = { async send() {} } as unknown as PromptSession;
    await expect(new PromptWaiter(session).waitForPrompt({ timeoutMs: 5 })).rejects.toThrow(/No prompt detected/);
  });

  it("treats a missing detection payload as no prompt", async () => {
    const session = new ScriptedSession([{ screen: "s", prompt_detected: null }, prompt()]);
    expect((await new PromptWaiter(session).waitForPrompt()).promptId).toBe("main_menu");
  });

  it("treats a detection with no id as an empty id", async () => {
    const session = new ScriptedSession([{ screen: "s", prompt_detected: { is_idle: true } }]);
    expect((await new PromptWaiter(session).waitForPrompt()).promptId).toBe("");
  });
});
