//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Declarative flow execution over prompt-detection rules.
 *
 * Port of the Python module `provide.uterm.detection.flow`.
 *
 * A flow is a scripted conversation — log in, answer the prompts, stop. This
 * looks at one screen and says what to send next, so a wrong answer types the
 * wrong thing into a live terminal.
 */

import { createHash } from "node:crypto";
import { compilePySearch } from "../pycompat/index.ts";
import { type DetectorSnapshot, PromptDetector, type PromptMatch } from "./detector.ts";
import { extractKV } from "./extractor.ts";
import { type ActionRule, type RuleSet, toPromptPatterns } from "./rules.ts";

/** What the engine decided for one screen. */
export interface FlowStep {
  /** Which flow was asked. */
  flowId: string;
  /** The prompt it believes the screen is showing. */
  currentPromptId: string | undefined;
  /** What to send, if anything. */
  nextAction: string | undefined;
  /** Whether the flow has finished. */
  done: boolean;
  /** Whatever the matched prompt asked to be pulled off the screen. */
  kvData: Record<string, unknown>;
}

/** Raised when a flow is asked for by a name nothing answers to. */
export class UnknownFlowError extends Error {}

/** How far down a match sits, and how early it starts. */
type Rank = readonly [number, number];

/** Whether `left` outranks `right`. */
function outranks(left: Rank, right: Rank): boolean {
  return left[0] !== right[0] ? left[0] > right[0] : left[1] > right[1];
}

/** Advances named flows using prompt detectors and rule metadata. */
export class FlowEngine {
  readonly #flows = new Map<string, { id: string; steps: ActionRule[] }>();
  readonly #promptPatterns = new Map<string, Record<string, unknown>>();
  /**
   * One detector per prompt-id set.
   *
   * Not an optimisation detail. Login polls `advance` every fifth of a
   * second, and building a detector per call recompiles every pattern
   * thousands of times — megabytes of compile logs and CPU taken from the
   * process being driven. The patterns never change after construction, so a
   * detector for a given set is stable and reusable.
   */
  readonly #detectorCache = new Map<string, PromptDetector>();
  /** How many detectors have actually been constructed. */
  #detectorBuilds = 0;

  /**
   * How many distinct prompt-id sets have been given a detector.
   *
   * Exposed because the cache is load-bearing rather than incidental: it is
   * the difference between recompiling every pattern several times a second
   * and not, and a cache that quietly misses looks exactly like one that
   * works until something is slow.
   */
  get detectorCacheSize(): number {
    return this.#detectorCache.size;
  }

  /**
   * How many detectors have been built since this engine was made.
   *
   * The cache size alone cannot tell a working cache from one that rebuilds
   * and overwrites its own entry, which is the failure worth catching.
   */
  get detectorBuildCount(): number {
    return this.#detectorBuilds;
  }

  constructor(ruleSet: RuleSet) {
    for (const flow of ruleSet.flows) {
      this.#flows.set(flow.id, flow);
    }
    for (const pattern of toPromptPatterns(ruleSet)) {
      this.#promptPatterns.set(pattern.id as string, pattern);
    }
  }

  /**
   * What to send next for `flowId`, given the screen.
   *
   * When several of a flow's steps match at once — because the screen holds
   * scrollback and an earlier step's prompt is still visible above the live
   * one — the match sitting closest to the bottom wins. A tie on the same
   * line goes to the one that starts earlier.
   *
   * @throws {UnknownFlowError} When no flow answers to that name.
   */
  advance(flowId: string, screen: string, cursor?: readonly [number, number]): FlowStep {
    const flow = this.#flows.get(flowId);
    if (flow === undefined) {
      throw new UnknownFlowError(`unknown flow: ${flowId}`);
    }

    const snapshot = FlowEngine.snapshot(screen, cursor);
    const lastIndex = flow.steps.length - 1;
    let best: { rank: Rank; index: number; action: ActionRule; match: PromptMatch } | undefined;

    for (const [index, action] of flow.steps.entries()) {
      const match = this.#detectPrompt(snapshot, FlowEngine.#candidatePromptIds(action));
      if (match === undefined) {
        continue;
      }
      const rank = this.#matchPosition(screen, match.promptId);
      // Strictly greater, so a tie keeps the earlier step — rule order is the
      // author's priority and applies here too.
      if (best === undefined || outranks(rank, best.rank)) {
        best = { rank, index, action, match };
      }
    }

    if (best === undefined) {
      return { flowId: flow.id, currentPromptId: undefined, nextAction: undefined, done: false, kvData: {} };
    }

    const pattern = this.#promptPatterns.get(best.match.promptId) as Record<string, unknown>;
    const kvData = extractKV(screen, pattern.kv_extract as never) ?? {};
    const terminal = FlowEngine.#isTerminal(best.action, best.index === lastIndex);
    return {
      flowId: flow.id,
      currentPromptId: best.match.promptId,
      // Keys travel only from a step that sends them, and never from one that
      // has just ended the flow.
      nextAction: terminal ? undefined : best.action.kind === "send_keys" ? (best.action.keys ?? undefined) : undefined,
      done: terminal,
      kvData,
    };
  }

  /**
   * How far down the screen a prompt's own pattern matches, and how early.
   *
   * The end offset ranks first and larger wins, so a live prompt at the
   * bottom beats a stale one in scrollback. Two matches ending at the same
   * offset are on the same line, and there the *earlier* start wins: a
   * whole-line `Enter your password:` and a generic `password[?:]\s*$` end at
   * the same column, and the anchored, longer match has to win or the vague
   * suffix steals the resolution.
   *
   * The detector only offers candidates it already matched, but an
   * end-anchored pattern can match the detector's tail *region* and find
   * nothing in the whole screen once trailing content shifts the anchor. Such
   * a prompt is at the tail by definition, so an empty search ranks there
   * rather than failing.
   */
  #matchPosition(screen: string, promptId: string): Rank {
    const pattern = (this.#promptPatterns.get(promptId) as Record<string, unknown>).regex as string;
    // Global, and deliberately *not* multiline: the reference ranks with a
    // bare `re.finditer`, so `^` here means the start of the screen and not
    // the start of a line. A line-anchored rule below the first line
    // therefore finds nothing and falls back to the tail — which is where it
    // is, so the answer is right for a roundabout reason. Adding multiline
    // looks like a fix and changes which prompt wins.
    const compiled = new RegExp(compilePySearch(pattern).source, "g");
    let best: Rank | undefined;
    // A global scan yields non-overlapping matches in order, so each one ends
    // later than the last and the final one is the lowest. The reference
    // spells this as a max over the same sequence; taken one pattern at a
    // time the two are the same walk. The start tie-break in the ranking key
    // only ever decides between *different* patterns.
    for (const hit of screen.matchAll(compiled)) {
      best = [hit.index + hit[0].length, -hit.index];
    }
    return best ?? [screen.length, 0];
  }

  /** The prompts a step will accept, gates first. */
  static #candidatePromptIds(action: ActionRule): string[] {
    const candidates = [...action.gate_prompts];
    // Appended last, so gate order is preserved and a step still recognises
    // the prompt it is waiting to arrive at.
    //
    // Neither the emptiness test nor the de-duplication changes an answer:
    // an id that names no rule is filtered out below, and a repeated one
    // compiles to the same pattern twice. Both are the reference's, and both
    // say something about what a candidate list is meant to hold.
    if (action.expects_prompt !== null && action.expects_prompt !== "" && !candidates.includes(action.expects_prompt)) {
      candidates.push(action.expects_prompt);
    }
    return candidates;
  }

  /** Detect one of a named set of prompts, reusing the detector. */
  #detectPrompt(snapshot: DetectorSnapshot, promptIds: string[]): PromptMatch | undefined {
    if (promptIds.length === 0) {
      // Nothing to wait for is nothing to match. Firing on any screen would
      // send keys at whatever happened to be showing. The empty-pattern check
      // below would reach the same answer; this one says that a step with no
      // gates was never a candidate, rather than one whose gates went missing.
      return undefined;
    }
    const key = JSON.stringify(promptIds);
    let detector = this.#detectorCache.get(key);
    if (detector === undefined) {
      const patterns = promptIds
        .map((promptId) => this.#promptPatterns.get(promptId))
        .filter((pattern): pattern is Record<string, unknown> => pattern !== undefined);
      if (patterns.length === 0) {
        // A typo in a rules file leaves the step inert rather than matching
        // everything.
        return undefined;
      }
      detector = new PromptDetector(patterns);
      this.#detectorBuilds += 1;
      this.#detectorCache.set(key, detector);
    }
    return detector.detectPrompt(snapshot);
  }

  /** Whether reaching this step ends the flow. */
  static #isTerminal(action: ActionRule, isLast: boolean): boolean {
    if (action.kind === "noop") {
      return true;
    }
    // A last step with nothing to send is the end; a middle one is a wait.
    return isLast && action.keys === null;
  }

  /**
   * The snapshot a flow hands the detector.
   *
   * The cursor sits on the last line unless told otherwise, because a flow is
   * answering a prompt at the bottom — claiming the origin would put it above
   * the region and change which detector pass runs. The cursor is assumed to
   * be at the end, since a flow only asks about a screen it believes has
   * settled.
   */
  static snapshot(screen: string, cursor?: readonly [number, number]): DetectorSnapshot {
    return {
      screen,
      screen_hash: createHash("sha256").update(screen, "utf-8").digest("hex"),
      cursor_at_end: true,
      has_trailing_space: screen.endsWith(" "),
      cursor: cursor === undefined ? { x: 0, y: (screen.match(/\n/g) ?? []).length } : { x: cursor[0], y: cursor[1] },
    };
  }
}
