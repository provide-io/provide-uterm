//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type ExpectSession, findMatch, sendAndExpect } from "./index.ts";

interface ExpectGolden {
  default_timeout_ms: number;
  matches: Array<{
    name: string;
    screen: string;
    expect_text: string | null;
    expect_regex: string | null;
    matched: string | null;
  }>;
}

const golden = loadGolden<ExpectGolden>("expect_golden.json");

/** A session whose screen the test advances step by step. */
class ScriptedSession implements ExpectSession {
  readonly sent: string[] = [];
  readonly waits: Array<{ timeoutMs: number; since: number | undefined }> = [];
  readonly #screens: string[];
  readonly #changes: boolean[];
  #seq = 0;
  #index = 0;

  /**
   * @param screens Screens returned in order; the last one repeats.
   * @param changes Whether each wait reports a change.
   */
  constructor(screens: string[], changes: boolean[] = []) {
    this.#screens = screens;
    this.#changes = changes;
  }

  async send(data: string): Promise<void> {
    this.sent.push(data);
  }

  snapshot(): Record<string, unknown> {
    if (this.#screens.length === 0) {
      return {};
    }
    return { screen: this.#screens[Math.min(this.#index, this.#screens.length - 1)] };
  }

  screenChangeSeq(): number {
    return this.#seq;
  }

  async waitForScreenChange(options: { timeoutMs: number; since?: number }): Promise<boolean> {
    this.waits.push({ timeoutMs: options.timeoutMs, since: options.since });
    const changed = this.#changes[this.#index] ?? true;
    this.#index += 1;
    this.#seq += 1;
    return changed;
  }
}

describe("findMatch", () => {
  it.each(golden.matches)("$name", (record) => {
    expect(findMatch(record.screen, record.expect_text ?? undefined, record.expect_regex ?? undefined)).toBe(
      record.matched ?? undefined,
    );
  });

  it("prefers the literal over the pattern", () => {
    // The caller gets back the literal they asked for rather than whatever
    // the regex happened to capture, which is what makes the result usable
    // for logging what actually satisfied the wait.
    const record = golden.matches.find((entry) => entry.name === "both, text wins");
    expect(record?.matched).toBe("ready");
  });

  it("returns the whole regex match, not a group", () => {
    const record = golden.matches.find((entry) => entry.name === "regex returns the whole match");
    expect(record?.matched).toBe("user@host");
  });

  it("treats an empty guard as a match", () => {
    // And that match is the empty string, which is falsy — a caller testing
    // truthiness rather than presence would read a match as a miss.
    const record = golden.matches.find((entry) => entry.name === "empty text matches anything");
    expect(record?.matched).toBe("");
    expect(findMatch("anything", "")).toBe("");
    expect(findMatch("anything", "")).not.toBeUndefined();
  });
});

describe("sendAndExpect", () => {
  it("sanitises keystrokes before sending", async () => {
    // The same guard the rest of the input path uses; a caller should not be
    // able to slip a stray control byte through this door. Carriage returns
    // survive — they are the point of a keystroke send.
    const session = new ScriptedSession(["ready"]);
    await sendAndExpect(session, "a\u0000b\u0007\r", { expectText: "ready" });
    expect(session.sent).toStrictEqual(["ab\r"]);
  });

  it("can be asked not to sanitise", async () => {
    const session = new ScriptedSession(["ready"]);
    await sendAndExpect(session, "a\u0000b", { expectText: "ready", sanitize: false });
    expect(session.sent).toStrictEqual(["a\u0000b"]);
  });

  it("sends nothing for an empty payload", async () => {
    // So the same call can be used as a pure read or wait without emitting a
    // stray frame the far end would see as a keystroke.
    const session = new ScriptedSession(["ready"]);
    await sendAndExpect(session, "", { expectText: "ready" });
    expect(session.sent).toStrictEqual([]);
  });

  it("returns at once when the screen already matches", async () => {
    // No wait at all: the condition the caller asked about is already true.
    const session = new ScriptedSession(["ready"]);
    expect(await sendAndExpect(session, "x", { expectText: "ready" })).toStrictEqual({
      matched: true,
      matchedText: "ready",
      screen: "ready",
      timedOut: false,
    });
    expect(session.waits).toStrictEqual([]);
  });

  it("returns at once for an empty guard", async () => {
    // The empty match is falsy, so a presence check is the only thing that
    // gets this right — a truthiness check would fall through and wait for a
    // condition that is already satisfied.
    const session = new ScriptedSession(["anything"]);
    const result = await sendAndExpect(session, "x", { expectText: "", timeoutMs: 5000 });
    expect(result).toStrictEqual({ matched: true, matchedText: "", screen: "anything", timedOut: false });
    expect(session.waits).toStrictEqual([]);
  });

  it("waits for the screen to catch up", async () => {
    const session = new ScriptedSession(["booting", "still booting", "ready"]);
    const result = await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: 5000 });
    expect(result.matched).toBe(true);
    expect(result.screen).toBe("ready");
    expect(session.waits.length).toBeGreaterThan(1);
  });

  it("gives up when the screen stops changing", async () => {
    // A stalled session is not going to satisfy the guard, so there is no
    // point burning the rest of the timeout on it.
    const session = new ScriptedSession(["booting"], [false]);
    const result = await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: 5000 });
    expect(result).toStrictEqual({ matched: false, matchedText: undefined, screen: "booting", timedOut: true });
    expect(session.waits).toHaveLength(1);
  });

  it("times out when the screen keeps changing without matching", async () => {
    const session = new ScriptedSession(["a", "b", "c"]);
    const result = await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: 5 });
    expect(result.matched).toBe(false);
    expect(result.timedOut).toBe(true);
  });

  it("reports a timeout of zero without waiting", async () => {
    const session = new ScriptedSession(["booting"]);
    const result = await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: 0 });
    expect(result.timedOut).toBe(true);
    expect(session.waits).toStrictEqual([]);
  });

  it("treats a negative timeout as zero", async () => {
    const session = new ScriptedSession(["booting"]);
    expect((await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: -100 })).timedOut).toBe(true);
  });

  it("waits once and returns when there is no guard", async () => {
    // With nothing to match on, the call means "send this and let the screen
    // settle" — one wait, and not a timeout, because nothing failed.
    const session = new ScriptedSession(["before", "after"]);
    const result = await sendAndExpect(session, "x", { timeoutMs: 5000 });
    expect(result).toStrictEqual({ matched: false, matchedText: undefined, screen: "after", timedOut: false });
    expect(session.waits).toHaveLength(1);
  });

  it("passes the sequence it started from so a change is not missed", async () => {
    // Output can land between the send and the wait; without the sequence
    // the wait would sleep through a change that already happened.
    const session = new ScriptedSession(["booting", "ready"]);
    await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: 5000 });
    expect(session.waits[0]?.since).toBe(0);
  });

  it("advances the sequence between waits", async () => {
    const session = new ScriptedSession(["a", "b", "ready"]);
    await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: 5000 });
    expect(session.waits.map((wait) => wait.since)).toStrictEqual([0, 1]);
  });

  it("never asks for a wait shorter than a millisecond", async () => {
    // A zero-length wait would spin rather than yield.
    const session = new ScriptedSession(["a", "b"]);
    await sendAndExpect(session, "x", { expectText: "ready", timeoutMs: 1 });
    for (const wait of session.waits) {
      expect(wait.timeoutMs).toBeGreaterThanOrEqual(1);
    }
  });

  it("matches on a pattern", async () => {
    const session = new ScriptedSession(["booting", "exit code 0"]);
    const result = await sendAndExpect(session, "x", { expectRegex: String.raw`exit code \d`, timeoutMs: 5000 });
    expect(result.matched).toBe(true);
    expect(result.matchedText).toBe("exit code 0");
  });

  it("uses the reference default timeout", async () => {
    const session = new ScriptedSession(["booting"], [false]);
    await sendAndExpect(session, "x", { expectText: "ready" });
    expect(session.waits[0]?.timeoutMs).toBeLessThanOrEqual(golden.default_timeout_ms);
    expect(session.waits[0]?.timeoutMs).toBeGreaterThan(golden.default_timeout_ms - 1000);
  });

  it("reads the screen as empty when the snapshot has none", async () => {
    const session = new ScriptedSession([]);
    const result = await sendAndExpect(session, "x", { timeoutMs: 5 });
    expect(result.screen).toBe("");
  });
});
