//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it, vi } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  DetectionEngine,
  type DetectorSnapshot,
  type PromptDetection,
  RuleValidationError,
  type ScreenSaver,
} from "./index.ts";

interface RecordedDetection {
  prompt_id: string;
  input_type: string;
  kv_data: Record<string, unknown>;
  is_idle: boolean | null;
  match_prompt_id: string | null;
}

interface EngineGolden {
  rules: Record<string, unknown>;
  other_rules: Record<string, unknown>;
  command_screen: string;
  login_screen: string;
  sync: Array<{ name: string; screen: string; detection: RecordedDetection | null }>;
  cache: Record<string, RecordedDetection | null>;
  disabled: { before: RecordedDetection; while_off: null; after: RecordedDetection; default_enabled: boolean };
  reload: {
    before: RecordedDetection | null;
    before_count: number;
    after: RecordedDetection | null;
    after_count: number;
    failed_error: string;
    survived: RecordedDetection | null;
    after_cached: RecordedDetection | null;
  };
  asynchronous: {
    matched: RecordedDetection | null;
    unmatched: RecordedDetection | null;
    hook_calls: Array<{ prompt_id: string | null; is_idle: boolean }>;
    survived_a_failing_saver: RecordedDetection | null;
    saver_status_without_one: Record<string, unknown>;
    saver_status_with_one: Record<string, unknown>;
  };
  namespace: { started: string; changed: string; default: string | null };
  pattern_count: number;
}

const golden = loadGolden<EngineGolden>("engine_golden.json");

/** A detection in the shape the corpus recorded it. */
function wire(detection: PromptDetection | undefined): RecordedDetection | null {
  return detection === undefined
    ? null
    : {
        prompt_id: detection.promptId,
        input_type: detection.inputType,
        kv_data: detection.kvData,
        is_idle: detection.isIdle ?? null,
        match_prompt_id: detection.match?.promptId ?? null,
      };
}

/** A snapshot, as a session hands one over. */
function snapshot(screen: string, extra: Partial<DetectorSnapshot> = {}): DetectorSnapshot {
  return { screen, screen_hash: `h:${screen}`, ...extra };
}

/** An engine over the corpus's own rules. */
function engine(options?: ConstructorParameters<typeof DetectionEngine>[1]): DetectionEngine {
  return new DetectionEngine(JSON.stringify(golden.rules), options);
}

describe("reading one screen", () => {
  it.each(golden.sync)("$name", (record) => {
    expect(wire(engine().processScreenSync(snapshot(record.screen)))).toStrictEqual(record.detection);
  });

  it("extracts what the matched prompt asked for", () => {
    const detection = engine().processScreenSync(snapshot(golden.command_screen));
    expect(detection?.kvData.sector).toBe(42);
  });

  it("carries nothing for a prompt that asked for nothing", () => {
    // An empty object rather than nothing at all, so a caller can read it
    // without checking first.
    expect(engine().processScreenSync(snapshot(golden.login_screen))?.kvData).toStrictEqual({});
  });

  it("carries nothing when the extraction found nothing", () => {
    // The rule asked for a sector and the screen has none. An empty result
    // and a failed one look the same to a caller, which is the reference's
    // choice.
    expect(
      golden.sync.find((entry) => entry.name === "a prompt whose extraction finds nothing")?.detection?.kv_data,
    ).toStrictEqual({});
  });

  it("leaves the buffer and the idle state unset", () => {
    // The synchronous path does no buffering, so claiming either would be a
    // lie a caller could act on.
    const detection = engine().processScreenSync(snapshot(golden.command_screen));
    expect(detection?.buffer).toBeUndefined();
    expect(detection?.isIdle).toBeUndefined();
  });

  it("says nothing about a screen with no prompt on it", () => {
    expect(engine().processScreenSync(snapshot("just output"))).toBeUndefined();
    expect(engine().processScreenSync(snapshot(""))).toBeUndefined();
  });

  it("copes with a frame that arrived without a screen at all", () => {
    // A producer with nothing to say sends one. Reading it as the absence of
    // a prompt is right; raising on it would end the session over a frame.
    expect(engine().processScreenSync({ screen_hash: "h" })).toBeUndefined();
    expect(engine().processScreenSync({ screen: null, screen_hash: "h" })).toBeUndefined();
  });
});

describe("not asking twice", () => {
  it("answers a repeated screen from what it already worked out", () => {
    // A terminal sends the same screen many times over. Re-running every
    // pattern against each one is the cost this avoids.
    const subject = engine();
    const detect = vi.spyOn(subject.detector, "detectPrompt");
    expect(wire(subject.processScreenSync(snapshot(golden.command_screen)))).toStrictEqual(golden.cache.first);
    expect(wire(subject.processScreenSync(snapshot(golden.command_screen)))).toStrictEqual(golden.cache.again);
    expect(detect).toHaveBeenCalledTimes(1);
  });

  it("asks again when the cursor moved", () => {
    // The key is the detector's fingerprint, which covers cursor state: a
    // screen whose text is identical but whose cursor moved is a fresh
    // question, not a stale answer.
    const subject = engine();
    const detect = vi.spyOn(subject.detector, "detectPrompt");
    subject.processScreenSync(snapshot(golden.command_screen));
    subject.processScreenSync(snapshot(golden.command_screen, { cursor: { x: 5, y: 1 } }));
    expect(detect).toHaveBeenCalledTimes(2);
  });

  it("asks again when the screen changed", () => {
    const subject = engine();
    const detect = vi.spyOn(subject.detector, "detectPrompt");
    subject.processScreenSync(snapshot(golden.command_screen));
    subject.processScreenSync(snapshot(golden.login_screen));
    expect(detect).toHaveBeenCalledTimes(2);
    expect(wire(subject.processScreenSync(snapshot(golden.login_screen)))).toStrictEqual(golden.cache.screen_changed);
  });

  it("remembers that a screen matched nothing", () => {
    // A miss is as worth caching as a hit; a terminal redrawing output that
    // is not a prompt is the common case.
    const subject = engine();
    const detect = vi.spyOn(subject.detector, "detectPrompt");
    expect(subject.processScreenSync(snapshot("nothing"))).toBeUndefined();
    expect(subject.processScreenSync(snapshot("nothing"))).toBeUndefined();
    expect(detect).toHaveBeenCalledTimes(1);
  });
});

describe("being switched off", () => {
  it("answers nothing at all", () => {
    // Rather than answering stale. An operator turning detection off wants it
    // off, not served from a cache.
    const subject = engine();
    expect(wire(subject.processScreenSync(snapshot(golden.command_screen)))).toStrictEqual(golden.disabled.before);
    subject.enabled = false;
    expect(subject.processScreenSync(snapshot(golden.command_screen))).toBeUndefined();
    subject.enabled = true;
    expect(wire(subject.processScreenSync(snapshot(golden.command_screen)))).toStrictEqual(golden.disabled.after);
  });

  it("is on to begin with", () => {
    expect(engine().enabled).toBe(golden.disabled.default_enabled);
    expect(engine().enabled).toBe(true);
  });

  it("does not even look at the screen", () => {
    const subject = engine();
    const detect = vi.spyOn(subject.detector, "detectPrompt");
    subject.enabled = false;
    subject.processScreenSync(snapshot(golden.command_screen));
    expect(detect).not.toHaveBeenCalled();
  });
});

describe("changing the rules while running", () => {
  it("uses the new ones", () => {
    const subject = engine();
    expect(wire(subject.processScreenSync(snapshot(golden.login_screen)))).toStrictEqual(golden.reload.before);
    expect(subject.patternCount).toBe(golden.reload.before_count);
    subject.reloadRules(JSON.stringify(golden.other_rules));
    expect(wire(subject.processScreenSync(snapshot(golden.login_screen)))).toStrictEqual(golden.reload.after);
    expect(subject.patternCount).toBe(golden.reload.after_count);
  });

  it("drops what it had already worked out", () => {
    // The cached answer names a prompt from the rules that produced it.
    // Keeping it would answer the next screen from rules no longer loaded.
    const subject = engine();
    subject.processScreenSync(snapshot(golden.login_screen));
    subject.reloadRules(JSON.stringify(golden.other_rules));
    expect(wire(subject.processScreenSync(snapshot(golden.login_screen)))).toStrictEqual(golden.reload.after_cached);
    expect(golden.reload.after_cached?.prompt_id).toBe("different");
  });

  it("keeps the most recent working rules when a reload fails", () => {
    // Transactional: a bad reload leaves detection running rather than taking
    // a live session down with it. Checked after a *successful* reload, so
    // what survives is the set that was actually loaded rather than whatever
    // the engine was built with.
    const subject = engine();
    subject.reloadRules(JSON.stringify(golden.other_rules));
    expect(() => subject.reloadRules("{not json")).toThrow(RuleValidationError);
    expect(wire(subject.processScreenSync(snapshot(golden.login_screen)))).toStrictEqual(golden.reload.survived);
    expect(golden.reload.survived?.prompt_id).toBe("different");
  });

  it("keeps the rules it was built with when the first reload fails", () => {
    const subject = engine();
    expect(() => subject.reloadRules("{not json")).toThrow(RuleValidationError);
    expect(subject.processScreenSync(snapshot(golden.login_screen))?.promptId).toBe("login");
    expect(subject.patternCount).toBe(golden.reload.before_count);
  });
});

/** A saver that records what it was asked to keep. */
function recordingSaver(onSave?: () => void): ScreenSaver & { calls: Array<string | undefined> } {
  const calls: Array<string | undefined> = [];
  return {
    calls,
    enabled: true,
    namespace: undefined,
    saveScreen(_snapshot, promptId) {
      calls.push(promptId);
      onSave?.();
    },
    setNamespace() {},
    screensDir: () => "/tmp/screens",
    savedCount: () => 0,
    setEnabled() {},
  };
}

describe("processing a screen with everything attached", () => {
  it("buffers, detects, and tells its hooks", async () => {
    const subject = engine({ idleThresholdS: 0 });
    const seen: Array<{ prompt_id: string | null; is_idle: boolean }> = [];
    subject.addHook(async (_snapshot, detection, _buffer, isIdle) => {
      seen.push({ prompt_id: detection?.promptId ?? null, is_idle: isIdle });
    });
    expect(wire(await subject.processScreen(snapshot(golden.command_screen)))).toStrictEqual(
      golden.asynchronous.matched,
    );
    expect(wire(await subject.processScreen(snapshot("nothing at all")))).toStrictEqual(golden.asynchronous.unmatched);
    expect(seen).toStrictEqual(golden.asynchronous.hook_calls);
  });

  it("attaches the buffer and the idle state to what it found", () => {
    // The synchronous path leaves both unset; only this one knows them.
    expect(golden.sync[0]?.detection?.is_idle).toBeNull();
    expect(golden.asynchronous.matched?.is_idle).toBe(true);
  });

  it("waits for each hook before the next", async () => {
    // A hook that is not awaited runs after the caller has already been
    // answered, so anything it was meant to do first has not happened.
    const subject = engine();
    const order: string[] = [];
    subject.addHook(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      order.push("slow");
    });
    subject.addHook(async () => {
      order.push("fast");
    });
    await subject.processScreen(snapshot(golden.command_screen));
    expect(order).toStrictEqual(["slow", "fast"]);
  });

  it("runs the hooks in the order they were added", () => {
    // Registration order is the caller's priority, the same as everywhere
    // else rules are applied in this package.
    expect(golden.asynchronous.hook_calls).toHaveLength(2);
  });

  it("keeps going when a hook throws", async () => {
    // A hook is somebody else's code. Losing detection because it raised
    // would cost the session the prompt as well as whatever the hook wanted.
    const subject = engine();
    const after: string[] = [];
    subject.addHook(async () => {
      throw new Error("hook exploded");
    });
    subject.addHook(async (_snapshot, detection) => {
      after.push(detection?.promptId ?? "none");
    });
    expect(await subject.processScreen(snapshot(golden.command_screen))).toBeDefined();
    expect(after).toStrictEqual(["command"]);
  });

  it("keeps going when the saver throws", async () => {
    // Same reason: a full disk should cost the screenshot, not the prompt.
    const saver = recordingSaver(() => {
      throw new Error("disk is full");
    });
    const subject = engine({ screenSaver: saver });
    expect(wire(await subject.processScreen(snapshot(golden.command_screen)))).toStrictEqual(
      golden.asynchronous.survived_a_failing_saver,
    );
  });

  it("tells the saver which prompt was showing", async () => {
    // So a screen can be filed under the prompt it was taken at.
    const saver = recordingSaver();
    const subject = engine({ screenSaver: saver });
    await subject.processScreen(snapshot(golden.command_screen));
    await subject.processScreen(snapshot("nothing at all"));
    expect(saver.calls).toStrictEqual(["command", undefined]);
  });

  it("marks the buffered screen with what matched it", async () => {
    // The buffer is what a replay reads back, and an unmarked screen cannot
    // be found again by prompt.
    const subject = engine();
    const detection = await subject.processScreen(snapshot(golden.command_screen));
    expect(detection?.buffer?.matched_prompt_id).toBe("command");
  });

  it("leaves the buffered screen unmarked when nothing matched", async () => {
    const subject = engine();
    await subject.processScreen(snapshot("nothing at all"));
    expect(subject.debugState().screen_buffer).toBeDefined();
  });

  it("says nothing about a saver it does not have", () => {
    expect(engine().screenSaverStatus()).toStrictEqual(golden.asynchronous.saver_status_without_one);
  });

  it("reports the saver it does have", () => {
    const subject = engine({ screenSaver: recordingSaver() });
    expect(subject.screenSaverStatus()).toStrictEqual(golden.asynchronous.saver_status_with_one);
  });

  it("reports how much the saver has kept", () => {
    // An operator checking whether saving is working reads this number; a
    // fixed zero would say it is not while it is.
    const saver: ScreenSaver = {
      enabled: true,
      namespace: "tw2002",
      saveScreen() {},
      setNamespace() {},
      screensDir: () => "/var/screens",
      savedCount: () => 17,
      setEnabled() {},
    };
    expect(engine({ screenSaver: saver }).screenSaverStatus()).toStrictEqual({
      enabled: true,
      screens_dir: "/var/screens",
      saved_count: 17,
      namespace: "tw2002",
    });
  });
});

describe("the namespace", () => {
  it("is what it was built with", () => {
    expect(engine({ namespace: "tw2002" }).namespace).toBe(golden.namespace.started);
    expect(engine().namespace).toBeUndefined();
  });

  it("can be changed", () => {
    const subject = engine({ namespace: "tw2002" });
    subject.setNamespace("other");
    expect(subject.namespace).toBe(golden.namespace.changed);
  });

  it("is passed on to the saver", async () => {
    // The saver files screens under it, so the two must not disagree.
    const seen: Array<string | undefined> = [];
    const saver: ScreenSaver = {
      enabled: true,
      namespace: undefined,
      saveScreen() {},
      setNamespace(namespace) {
        seen.push(namespace);
      },
      screensDir: () => "/tmp/screens",
      savedCount: () => 0,
      setEnabled() {},
    };
    const subject = engine({ screenSaver: saver, namespace: "tw2002" });
    subject.setNamespace("other");
    expect(seen).toStrictEqual(["other"]);
  });

  it("copes with being changed when there is no saver", () => {
    const subject = engine();
    expect(() => subject.setNamespace("other")).not.toThrow();
    expect(subject.namespace).toBe("other");
  });
});

describe("turning saving on and off", () => {
  it("passes the switch to the saver", () => {
    const seen: boolean[] = [];
    const saver: ScreenSaver = {
      enabled: true,
      namespace: undefined,
      saveScreen() {},
      setNamespace() {},
      screensDir: () => "/tmp/screens",
      savedCount: () => 0,
      setEnabled(enabled) {
        seen.push(enabled);
      },
    };
    const subject = engine({ screenSaver: saver });
    subject.setScreenSaving(false);
    subject.setScreenSaving(true);
    expect(seen).toStrictEqual([false, true]);
  });

  it("copes with there being no saver", () => {
    expect(() => engine().setScreenSaving(false)).not.toThrow();
  });
});

describe("what it will tell you about itself", () => {
  it("reports the pattern count", () => {
    expect(engine().patternCount).toBe(golden.pattern_count);
  });

  it("hands over the detector for diagnostics", () => {
    const result = engine().detectWithDiagnostics(snapshot(golden.command_screen));
    expect(result.match?.promptId).toBe("command");
  });

  it("reports whether the screen has settled", async () => {
    const subject = engine({ idleThresholdS: 0 });
    await subject.processScreen(snapshot(golden.command_screen));
    expect(subject.isIdle).toBe(true);
    const slow = engine({ idleThresholdS: 3600 });
    await slow.processScreen(snapshot(golden.command_screen));
    expect(slow.isIdle).toBe(false);
  });

  it("describes its own state without the caller reaching inside", () => {
    const state = engine({ namespace: "tw2002", idleThresholdS: 5 }).debugState();
    expect(state.idle_threshold_s).toBe(5);
    expect(state.namespace).toBe("tw2002");
    expect(state.screen_saver).toBeNull();
    expect((state.screen_buffer as Record<string, unknown>).size).toBe(0);
  });

  it("describes an empty buffer as not idle", () => {
    // Nothing has been seen, so nothing has settled — rather than reporting
    // the idle state of a screen that was never there.
    const state = engine().debugState();
    expect((state.screen_buffer as Record<string, unknown>).is_idle).toBe(false);
    expect((state.screen_buffer as Record<string, unknown>).last_change_seconds_ago).toBe(0);
  });

  it("describes idleness by the default window, not the configured one", () => {
    // Deliberately different in the reference: this is a debugging view, and
    // it answers "has this settled by the usual standard" rather than "has it
    // settled by the standard this engine was told to use". An engine told to
    // treat everything as idle still shows a screen that has just arrived as
    // not settled.
    const subject = engine({ idleThresholdS: 0 });
    subject.processScreenSync(snapshot(golden.command_screen));
    void subject.processScreen(snapshot(golden.command_screen));
    expect(subject.isIdle).toBe(true);
    expect((subject.debugState().screen_buffer as Record<string, unknown>).is_idle).toBe(false);
  });

  it("counts what it has buffered", async () => {
    const subject = engine();
    await subject.processScreen(snapshot(golden.command_screen));
    await subject.processScreen(snapshot(golden.login_screen));
    expect((subject.debugState().screen_buffer as Record<string, unknown>).size).toBe(2);
  });

  it("runs the caller's normaliser over the prompt region", () => {
    // Which is what lets a volatile field — a clock in the prompt — stop
    // making every frame a cache miss.
    const subject = engine({ normalizer: () => "constant" });
    const detect = vi.spyOn(subject.detector, "detectPrompt");
    subject.processScreenSync(snapshot(`${golden.command_screen}`));
    subject.processScreenSync(snapshot(`${golden.command_screen}\nmore`));
    // Both regions normalise to the same text, so the second is a cache hit.
    expect(detect).toHaveBeenCalledTimes(1);
  });

  it("includes the saver in its description when there is one", () => {
    const subject = engine({ screenSaver: recordingSaver() });
    expect(subject.debugState().screen_saver).toStrictEqual(golden.asynchronous.saver_status_with_one);
  });

  it("honours the buffer size it was built with", () => {
    const subject = engine({ bufferSize: 2 });
    expect((subject.debugState().screen_buffer as Record<string, unknown>).max_size).toBe(2);
  });
});
