//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Rule-based prompt detection and data extraction.
 *
 * Port of the Python module `provide.uterm.detection.engine`.
 *
 * This is what a session actually calls: hand it a screen, get back what
 * prompt is showing and what was on it. Everything else in detection hangs
 * off the one method.
 */

import { BufferManager, type ScreenBuffer } from "./buffer.ts";
import {
  type DetectorSnapshot,
  type PromptDetectionDiagnostics,
  PromptDetector,
  type PromptMatch,
} from "./detector.ts";
import { extractKV } from "./extractor.ts";
import { loadRuleSet } from "./loader.ts";
import { type RuleSet, toPromptPatterns } from "./rules.ts";

/** What the engine concluded about one screen. */
export interface PromptDetection {
  promptId: string;
  inputType: string;
  kvData: Record<string, unknown>;
  match: PromptMatch | undefined;
  isIdle: boolean | undefined;
  buffer: ScreenBuffer | undefined;
}

/** Somewhere to put screens, for whoever wants them later. */
export interface ScreenSaver {
  saveScreen(snapshot: DetectorSnapshot, promptId: string | undefined): void;
  setNamespace(namespace: string | undefined): void;
  screensDir(): string;
  savedCount(): number;
  setEnabled(enabled: boolean): void;
  readonly enabled: boolean;
  readonly namespace: string | undefined;
}

/** Called after every screen the engine processes. */
export type DetectionHook = (
  snapshot: DetectorSnapshot,
  detection: PromptDetection | undefined,
  buffer: ScreenBuffer,
  isIdle: boolean,
) => Promise<void>;

/** Options for {@link DetectionEngine}. */
export interface DetectionEngineOptions {
  normalizer?: (regionText: string) => string;
  bufferSize?: number;
  idleThresholdS?: number;
  screenSaver?: ScreenSaver;
  namespace?: string;
}

/** Detection, buffering, saving and hooks for one session. */
export class DetectionEngine {
  readonly #normalizer: ((regionText: string) => string) | undefined;
  readonly #bufferManager: BufferManager;
  readonly #idleThresholdS: number;
  readonly #screenSaver: ScreenSaver | undefined;
  readonly #hooks: DetectionHook[] = [];
  #detector: PromptDetector;
  #namespace: string | undefined;
  #enabled = true;
  /** The fingerprint the last answer was worked out for. */
  #lastFingerprint = "";
  /** That answer, kept so an unchanged screen is not re-read. */
  #lastMatch: PromptMatch | undefined;

  constructor(rules: RuleSet | string, options: DetectionEngineOptions = {}) {
    this.#normalizer = options.normalizer;
    this.#detector = DetectionEngine.#compile(rules, this.#normalizer);
    this.#bufferManager = new BufferManager(options.bufferSize ?? 50);
    this.#idleThresholdS = options.idleThresholdS ?? 2.0;
    this.#screenSaver = options.screenSaver;
    this.#namespace = options.namespace;
  }

  /** Load rules and compile a detector from them. */
  static #compile(rules: RuleSet | string, normalizer: ((text: string) => string) | undefined): PromptDetector {
    const patterns = toPromptPatterns(loadRuleSet(rules));
    return new PromptDetector(patterns, normalizer === undefined ? {} : { normalizer });
  }

  /**
   * Read one screen. Pure work, no waiting.
   *
   * The answer is cached against the detector's fingerprint. A terminal sends
   * the same screen many times over, and re-running every pattern against
   * each one is the cost this avoids. The fingerprint covers cursor state, so
   * a screen whose text is identical but whose cursor moved is a fresh
   * question rather than a stale answer.
   */
  processScreenSync(snapshot: DetectorSnapshot): PromptDetection | undefined {
    if (!this.#enabled) {
      // Nothing at all rather than something stale: an operator turning
      // detection off wants it off, not served from a cache.
      return undefined;
    }

    // Read once: a snapshot with no screen at all is a frame that arrived
    // empty, and it reads the same as an empty one everywhere below.
    const screen = snapshot.screen ?? "";
    const fingerprint = this.#detector.promptFingerprint(snapshot);
    let promptMatch: PromptMatch | undefined;
    // The emptiness test is the reference's and cannot fire — a fingerprint is
    // always a digest and some cursor state. It is what makes the empty string
    // usable as "nothing cached", which is how a reload clears the cache.
    if (fingerprint !== "" && fingerprint === this.#lastFingerprint) {
      promptMatch = this.#lastMatch;
    } else {
      promptMatch = this.#detector.detectPrompt(snapshot);
      this.#lastFingerprint = fingerprint;
      // A miss is cached as readily as a hit — a terminal redrawing output
      // that is not a prompt is the common case.
      this.#lastMatch = promptMatch;
    }

    if (promptMatch === undefined) {
      return undefined;
    }

    let kvData: Record<string, unknown> = {};
    // Extraction on an absent configuration produces nothing anyway, so this
    // changes no answer. It is the reference's, and it says that a rule
    // asking for nothing is different from one whose search came up empty.
    if (promptMatch.kvExtract !== undefined && promptMatch.kvExtract !== null) {
      kvData = extractKV(screen, promptMatch.kvExtract as never) ?? {};
    }

    return {
      promptId: promptMatch.promptId,
      inputType: promptMatch.inputType,
      kvData,
      match: promptMatch,
      // Both are the asynchronous path's to fill in; it is the only one that
      // knows them.
      isIdle: undefined,
      buffer: undefined,
    };
  }

  /**
   * Read one screen, buffering it, saving it and telling the hooks.
   *
   * A saver that raises and a hook that throws are both stepped over. Neither
   * is the reason the session exists, and taking detection down with them
   * would cost the prompt as well as whatever they wanted.
   */
  async processScreen(snapshot: DetectorSnapshot): Promise<PromptDetection | undefined> {
    const buffer = this.#bufferManager.addScreen(snapshot as never);
    const isIdle = this.#bufferManager.detectIdleState(this.#idleThresholdS);

    const detection = this.processScreenSync(snapshot);
    if (detection?.match !== undefined) {
      // Marked so a replay can find the screen again by the prompt it was
      // taken at.
      buffer.matched_prompt_id = detection.match.promptId;
    }

    if (this.#screenSaver !== undefined) {
      try {
        this.#screenSaver.saveScreen(snapshot, detection?.promptId);
      } catch {
        // A full disk costs the screenshot, not the prompt.
      }
    }

    if (detection !== undefined) {
      detection.isIdle = isIdle;
      detection.buffer = buffer;
    }

    for (const hook of this.#hooks) {
      try {
        await hook(snapshot, detection, buffer, isIdle);
      } catch {
        // Somebody else's code. One that raises must not stop the next one,
        // nor the answer going back to the caller.
      }
    }

    return detection;
  }

  /** Register something to run after every screen. */
  addHook(hook: DetectionHook): void {
    this.#hooks.push(hook);
  }

  /** Read a screen and say why anything nearly matched. */
  detectWithDiagnostics(snapshot: DetectorSnapshot): PromptDetectionDiagnostics {
    return this.#detector.detectPromptWithDiagnostics(snapshot);
  }

  /**
   * Swap the rules for new ones.
   *
   * Transactional: rules that will not load leave the old ones running rather
   * than taking a live session down. The cached answer goes with them, since
   * it names a prompt from the rules that produced it.
   *
   * @throws {RuleValidationError} When the new rules cannot be loaded.
   */
  reloadRules(rules: RuleSet | string): void {
    const detector = DetectionEngine.#compile(rules, this.#normalizer);
    this.#detector = detector;
    // Clearing the fingerprint is what empties the cache; clearing the match
    // as well changes nothing, and says the answer went with the rules that
    // produced it rather than lingering unreachable.
    this.#lastFingerprint = "";
    this.#lastMatch = undefined;
  }

  /** The detector underneath, for whoever wants to ask it directly. */
  get detector(): PromptDetector {
    return this.#detector;
  }

  /** How many rules are loaded. */
  get patternCount(): number {
    return this.#detector.patternCount;
  }

  /** Whether screens are read at all. */
  get enabled(): boolean {
    return this.#enabled;
  }

  set enabled(value: boolean) {
    this.#enabled = value;
  }

  /** Whether the screen has been still for long enough. */
  get isIdle(): boolean {
    return this.#bufferManager.detectIdleState(this.#idleThresholdS);
  }

  /** What this session is called, for whoever files its screens. */
  get namespace(): string | undefined {
    return this.#namespace;
  }

  /** Rename the session, telling the saver so the two do not disagree. */
  setNamespace(namespace: string | undefined): void {
    this.#namespace = namespace;
    this.#screenSaver?.setNamespace(namespace);
  }

  /** What the saver is doing, if there is one. */
  screenSaverStatus(): Record<string, unknown> {
    if (this.#screenSaver === undefined) {
      return { enabled: false };
    }
    return {
      enabled: this.#screenSaver.enabled,
      screens_dir: this.#screenSaver.screensDir(),
      saved_count: this.#screenSaver.savedCount(),
      namespace: this.#screenSaver.namespace ?? null,
    };
  }

  /** Turn screen saving on or off. */
  setScreenSaving(enabled: boolean): void {
    this.#screenSaver?.setEnabled(enabled);
  }

  /** Everything a caller might otherwise reach inside for. */
  debugState(): Record<string, unknown> {
    const recent = this.#bufferManager.getRecent(1);
    const newest = recent[0];
    return {
      idle_threshold_s: this.#idleThresholdS,
      namespace: this.#namespace ?? null,
      screen_buffer: {
        size: this.#bufferManager.size,
        max_size: this.#bufferManager.maxSize,
        // Nothing seen is nothing settled. The buffer says the same on its
        // own — it has no change time to measure from — so this guard changes
        // no answer; it states the intent rather than relying on the other
        // module to keep it.
        is_idle: newest === undefined ? false : this.#bufferManager.detectIdleState(),
        last_change_seconds_ago: newest?.time_since_last_change ?? 0.0,
      },
      screen_saver: this.#screenSaver === undefined ? null : this.screenSaverStatus(),
    };
  }
}
