//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadGolden, SPEC_DIR } from "../testing/golden.ts";
import { FlowEngine, loadRuleSet, parseRuleSet, UnknownFlowError } from "./index.ts";

interface RecordedStep {
  flow_id: string;
  current_prompt_id: string | null;
  next_action: string | null;
  done: boolean;
  kv_data: Record<string, unknown>;
}

interface FlowGolden {
  rules: Record<string, unknown>;
  scrollback: string;
  cases: Array<{
    name: string;
    flow_id: string;
    screen: string;
    cursor: [number, number] | null;
    step: RecordedStep;
  }>;
  unknown_flow_error: string;
  cached_same_object: boolean;
  snapshot: Record<string, Record<string, unknown>>;
}

const golden = loadGolden<FlowGolden>("flow_golden.json");

/** An engine over the corpus's own rules. */
function engine(): FlowEngine {
  return new FlowEngine(parseRuleSet(golden.rules));
}

/** A decision in the shape the corpus recorded it. */
function wire(step: ReturnType<FlowEngine["advance"]>): RecordedStep {
  return {
    flow_id: step.flowId,
    current_prompt_id: step.currentPromptId ?? null,
    next_action: step.nextAction ?? null,
    done: step.done,
    kv_data: step.kvData,
  };
}

/** The recorded case with this name. */
function flowCase(name: string) {
  return golden.cases.find((entry) => entry.name === name);
}

describe("deciding what to send next", () => {
  it.each(golden.cases)("$name", (record) => {
    expect(wire(engine().advance(record.flow_id, record.screen, record.cursor ?? undefined))).toStrictEqual(
      record.step,
    );
  });

  it("walks a flow prompt by prompt", () => {
    const subject = engine();
    expect(subject.advance("login", "Enter your name: ").nextAction).toBe("player\r");
    expect(subject.advance("login", "Enter your password: ").nextAction).toBe("secret\r");
    expect(subject.advance("login", "Command [TL=00:00:00]:? ").done).toBe(true);
  });

  it("says nothing when no step's prompt is on screen", () => {
    // Not done and nothing to send: the caller waits rather than guessing.
    for (const name of ["nothing matches", "an empty screen"]) {
      const step = flowCase(name)?.step;
      expect(step?.current_prompt_id).toBeNull();
      expect(step?.next_action).toBeNull();
      expect(step?.done).toBe(false);
    }
  });

  it("refuses a flow it does not have", () => {
    // Named, because a caller passing the wrong id needs to see which one.
    expect(() => engine().advance("nonexistent", "screen")).toThrow(UnknownFlowError);
    expect(() => engine().advance("nonexistent", "screen")).toThrow(/nonexistent/);
    expect(golden.unknown_flow_error).toContain("nonexistent");
  });

  it("copes with a flow that has no steps", () => {
    expect(flowCase("a flow with no steps")?.step.current_prompt_id).toBeNull();
  });
});

describe("choosing between steps that all match", () => {
  it("takes the prompt closest to the bottom", () => {
    // A screen holds scrollback, so an earlier step's prompt is often still
    // visible above the live one. Taking the first matching step would answer
    // a prompt that scrolled past minutes ago.
    expect(golden.scrollback).toContain("Enter your name:");
    expect(flowCase("a stale prompt above a live one")?.step.current_prompt_id).toBe("command");
  });

  it("takes it even when the earlier step is the lower one", () => {
    // Position decides, not step order — so the last matching step is not the
    // answer either.
    const step = flowCase("the earlier step's prompt is the lower one")?.step;
    expect(step?.current_prompt_id).toBe("command");
    expect(step?.next_action).toBe("L");
  });

  it("keeps the earlier step when two tie exactly", () => {
    // Two steps gated on one prompt rank identically. Rule order is the
    // author's priority, so the first one written wins.
    expect(flowCase("two steps tied on the same prompt")?.step.next_action).toBe("1");
  });

  it("ranks by the lowest of several matches of one pattern", () => {
    // The same label appears twice, with a competing step's prompt between
    // them. Ranking by the first occurrence would hand the answer to the
    // competitor.
    expect(flowCase("a prompt appearing twice on one screen")?.step.current_prompt_id).toBe("twice");
    expect(flowCase("one pattern matching twice, ranked by the lower")?.step.next_action).toBe("R");
  });

  it("ranks against the whole screen, not per line", () => {
    // The reference ranks with a bare finditer, so "^" here means the start of
    // the *screen*. A line-anchored rule below the first line finds nothing
    // and falls back to the tail — right answer, roundabout reason. Adding
    // multiline looks like a fix and changes which prompt wins: with it, two
    // such rules rank by position and the lower one takes the flow.
    expect(flowCase("a rule anchored to a line start")?.step.current_prompt_id).toBe("line_start");
    expect(flowCase("a line-anchored rule outranks one above it")?.step.next_action).toBe("C");
    expect(flowCase("two line-anchored rules, neither on the first line")?.step.next_action).toBe("A");
  });

  it("ranks without folding case", () => {
    // The detector is case-sensitive, so ranking must not reach for
    // lower-case text it would never have matched. Folding case here would
    // find the occurrence at the bottom and beat the competitor between them.
    expect(flowCase("a case-sensitive rule ranked without case folding")?.step.next_action).toBe("E");
  });

  it("breaks a same-line tie toward the earlier start", () => {
    // A whole-line "Enter your password:" and a generic "password[?:]\\s*$"
    // end at the same column. The anchored, longer match has to win, or the
    // vague suffix steals the resolution and the flow answers as though it
    // were at a different prompt.
    const step = flowCase("two rules ending on the same line")?.step;
    expect(step?.current_prompt_id).toBe("login_pass");
    expect(step?.next_action).toBe("a");
  });

  it("falls back to the tail for a rule that anchors to the end", () => {
    // An end-anchored pattern matches the detector's tail *region* and finds
    // nothing in the whole screen once trailing blank lines shift the anchor.
    // Ranking has to survive that rather than failing on an empty search, and
    // has to rank it at the tail rather than at the origin — where a stale
    // match above it would beat it.
    expect(flowCase("an end-anchored rule at the tail")?.step.current_prompt_id).toBe("anchored");
    expect(flowCase("an end-anchored rule with trailing blank lines")?.step.current_prompt_id).toBe("anchored");
    expect(flowCase("an end-anchored rule with trailing blank lines")?.step.next_action).toBe("r");
    // And ranked *at the tail*, not at the origin: a competitor matching
    // above it must not win.
    expect(flowCase("an end-anchored rule outranks one above it")?.step.next_action).toBe("A");
  });
});

describe("which prompts a step waits for", () => {
  it("uses the prompt a step says it expects", () => {
    expect(flowCase("a step naming its expected prompt")?.step.current_prompt_id).toBe("command");
  });

  it("puts the gates before the expectation", () => {
    // Two prompts matching on one line resolve by rule order inside the
    // detector, so which is listed first decides the answer.
    expect(flowCase("gate order deciding between two on one line")?.step.current_prompt_id).toBe("pass_suffix");
    expect(flowCase("the same two, the other way round")?.step.current_prompt_id).toBe("login_pass");
  });

  it("uses the gates and the expectation together", () => {
    // Both are candidates, so a step gated on one prompt still recognises the
    // one it is waiting to arrive at.
    expect(flowCase("gates and an expectation together, the gate matching")?.step.current_prompt_id).toBe("login_name");
    expect(flowCase("gates and an expectation together, the expectation matching")?.step.current_prompt_id).toBe(
      "command",
    );
  });

  it("skips a step that names no prompt at all", () => {
    // Nothing to wait for is nothing to match; firing it on any screen would
    // send keys at whatever happened to be showing.
    expect(flowCase("a step with nothing to wait for")?.step.current_prompt_id).toBeNull();
  });

  it("skips a step naming a prompt the rules do not define", () => {
    // A typo in a rules file leaves the step inert rather than matching
    // everything.
    expect(flowCase("a step naming a prompt that does not exist")?.step.current_prompt_id).toBeNull();
  });
});

describe("deciding a flow is over", () => {
  it("ends on a no-op step", () => {
    const step = flowCase("a no-op step")?.step;
    expect(step?.done).toBe(true);
    expect(step?.next_action).toBeNull();
  });

  it("ends on a no-op wherever it sits", () => {
    // Not only as the last step: a no-op is the author saying "stop here".
    expect(flowCase("a no-op that is not the last step")?.step.done).toBe(true);
  });

  it("sends nothing from a send step that carries no keys", () => {
    // A send step with nothing written to send is a rules-file omission, not
    // an instruction to send the empty string.
    const step = flowCase("a middle send step with nothing to send")?.step;
    expect(step?.done).toBe(false);
    expect(step?.next_action).toBeNull();
  });

  it("sends nothing from a last send step that has nothing to send", () => {
    // Such a step is terminal *and* a send, which is the one arrangement
    // where both guards on the outgoing keys matter at once.
    const step = flowCase("a last send step with nothing to send")?.step;
    expect(step?.done).toBe(true);
    expect(step?.next_action).toBeNull();
  });

  it("sends nothing from a step that ended the flow", () => {
    // A no-op carrying keys is a rules-file mistake, and sending them would
    // type into a session the flow has just declared finished.
    const step = flowCase("a no-op that still carries keys")?.step;
    expect(step?.done).toBe(true);
    expect(step?.next_action).toBeNull();
  });

  it("sends nothing from a step that is not a send", () => {
    // A wait step may carry keys; they are still not sent.
    const step = flowCase("a middle wait step carrying keys")?.step;
    expect(step?.done).toBe(false);
    expect(step?.next_action).toBeNull();
  });

  it("ends on a last step with nothing to send", () => {
    expect(flowCase("a final step with nothing to send")?.step.done).toBe(true);
  });

  it("does not end on a middle step with nothing to send", () => {
    // There are more steps after it, so the flow is waiting rather than over.
    const step = flowCase("a middle step with nothing to send")?.step;
    expect(step?.done).toBe(false);
    expect(step?.next_action).toBeNull();
  });
});

describe("values carried out with the decision", () => {
  it("extracts what the matched prompt asks for", () => {
    expect(flowCase("extracted values travel with the step")?.step.kv_data.attempt).toBe(3);
  });

  it("takes the last of several", () => {
    // Same reason as everywhere else: the screen holds history.
    expect(flowCase("the last extracted value wins")?.step.kv_data.attempt).toBe(7);
  });

  it("carries nothing for a prompt that asks for nothing", () => {
    // An empty object rather than nothing at all, so a caller can read it
    // without checking first.
    expect(flowCase("a prompt with nothing to extract")?.step.kv_data).toStrictEqual({});
  });
});

describe("the snapshot a flow builds", () => {
  it.each(Object.entries(golden.snapshot))("%s", (_name, expected) => {
    const screen = expected.screen as string;
    const cursor = expected.cursor as { x: number; y: number };
    const given = _name === "with_cursor" ? ([cursor.x, cursor.y] as const) : undefined;
    expect(FlowEngine.snapshot(screen, given)).toStrictEqual(expected);
  });

  it("puts the cursor on the last line when it is not told one", () => {
    // A flow is answering a prompt at the bottom, so that is where the cursor
    // is; claiming the origin would put it above the region and change which
    // detector pass runs.
    expect(FlowEngine.snapshot("one\ntwo\n").cursor).toStrictEqual({ x: 0, y: 2 });
  });

  it("uses the cursor it is given", () => {
    expect(FlowEngine.snapshot("one\ntwo\n", [3, 1]).cursor).toStrictEqual({ x: 3, y: 1 });
  });

  it("assumes the cursor is at the end", () => {
    // A flow only asks about a screen it believes is settled, so the
    // detector's cursor heuristic must not hold its patterns back.
    expect(FlowEngine.snapshot("one").cursor_at_end).toBe(true);
  });

  it("notices a trailing space", () => {
    // Which is what tells the detector an input field is live.
    expect(FlowEngine.snapshot("prompt: ").has_trailing_space).toBe(true);
    expect(FlowEngine.snapshot("prompt:").has_trailing_space).toBe(false);
  });

  it("hashes the screen it was given", () => {
    expect(FlowEngine.snapshot("one\ntwo\n").screen_hash).toBe(golden.snapshot.no_cursor?.screen_hash);
    expect(FlowEngine.snapshot("").screen_hash).not.toBe(FlowEngine.snapshot("one").screen_hash);
  });

  it("gives an empty screen the origin", () => {
    expect(FlowEngine.snapshot("").cursor).toStrictEqual({ x: 0, y: 0 });
  });
});

describe("reusing detectors", () => {
  it("builds one detector, not one per call", () => {
    // The size alone cannot tell a working cache from one that rebuilds and
    // overwrites its own entry — which is the failure worth catching, since
    // login polls this every fifth of a second.
    const subject = engine();
    const screen = "Enter your name: ";
    subject.advance("login", screen);
    const afterFirst = subject.detectorBuildCount;
    subject.advance("login", screen);
    subject.advance("login", screen);
    expect(subject.detectorBuildCount).toBe(afterFirst);
  });

  it("does not rebuild one for the same prompts", () => {
    // Login polls this every fifth of a second. Rebuilding recompiles every
    // pattern thousands of times, which is invisible until something is slow
    // — so the cache is checked directly rather than by its answers, which
    // are the same either way.
    expect(golden.cached_same_object).toBe(true);
    const subject = engine();
    const screen = "Enter your name: ";
    subject.advance("login", screen);
    const afterFirst = subject.detectorCacheSize;
    expect(afterFirst).toBeGreaterThan(0);
    subject.advance("login", screen);
    subject.advance("login", screen);
    expect(subject.detectorCacheSize).toBe(afterFirst);
  });

  it("keeps different prompt sets apart", () => {
    // A cache keyed too loosely would answer one step's question with another
    // step's detector.
    const subject = engine();
    expect(subject.advance("login", "Enter your name: ").currentPromptId).toBe("login_name");
    expect(subject.advance("expects", "Command [TL=00:00:00]:? ").currentPromptId).toBe("command");
    expect(subject.detectorCacheSize).toBeGreaterThan(1);
  });

  it("keys the cache on the order the prompts were given", () => {
    // Order decides which rule wins inside a detector, so two steps listing
    // the same prompts differently must not share one.
    const subject = engine();
    subject.advance("order_matters", "Enter your password:");
    const afterFirst = subject.detectorCacheSize;
    subject.advance("order_reversed", "Enter your password:");
    expect(subject.detectorCacheSize).toBe(afterFirst + 1);
  });

  it("does not cache a step with nothing to wait for", () => {
    // Nor one naming a prompt that does not exist: neither can ever match, so
    // caching a detector for them would grow the map for nothing.
    const subject = engine();
    subject.advance("gateless", "Enter your name: ");
    expect(subject.detectorCacheSize).toBe(0);
    subject.advance("unknown_gate", "Enter your name: ");
    expect(subject.detectorCacheSize).toBe(0);
  });
});

describe("loading a rule set from wherever it lives", () => {
  it("parses JSON text", () => {
    expect(loadRuleSet(JSON.stringify(golden.rules))).toStrictEqual(parseRuleSet(golden.rules));
  });

  it("passes an already-parsed rule set straight through", () => {
    const parsed = parseRuleSet(golden.rules);
    expect(loadRuleSet(parsed)).toBe(parsed);
  });

  it("refuses text that is not JSON, and says so", () => {
    // Text that is not JSON and valid JSON that is not a rule set are
    // different mistakes, fixed differently.
    expect(() => loadRuleSet("{not json")).toThrow(/Failed to parse rules: not JSON/);
  });

  it("refuses JSON that is not a rule set", () => {
    expect(() => loadRuleSet('{"prompts": []}')).toThrow(/Failed to parse rules/);
  });

  it("refuses a file that is not there", () => {
    // Named, because an operator with several rules files needs to know which.
    expect(() => loadRuleSet("/nonexistent/rules.json", { fromFile: true })).toThrow(/Rules file not found/);
    expect(() => loadRuleSet("/nonexistent/rules.json", { fromFile: true })).toThrow(/\/nonexistent\/rules\.json/);
  });

  it("reads a rule set from a file", () => {
    // The path is read, not treated as JSON text.
    const path = join(SPEC_DIR, "..", "packages", "provide-uterm-ts", "testdata", "rules_on_disk.json");
    writeFileSync(path, JSON.stringify(golden.rules), "utf-8");
    try {
      expect(loadRuleSet(path, { fromFile: true })).toStrictEqual(parseRuleSet(golden.rules));
    } finally {
      rmSync(path);
    }
  });
});
