//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Waiting for prompts, and answering them.
 *
 * Port of the Python module `provide.uterm.io`.
 *
 * The pairing an automation lives on: block until the far end asks a
 * question, then answer it in the form that question expects.
 */

/** Default wait for a prompt, in milliseconds. */
export const DEFAULT_PROMPT_TIMEOUT_MS = 10_000;

/** Default polling backstop, in milliseconds. */
export const DEFAULT_PROMPT_READ_INTERVAL_MS = 250;

/**
 * Whether a prompt must be settled before it is accepted.
 *
 * A prompt drawn mid-repaint can be the wrong one, and answering a menu that
 * is still rendering sends the keystroke to whatever lands there instead.
 */
export const DEFAULT_PROMPT_REQUIRE_IDLE = true;

/**
 * Fraction of the timeout after which an unsettled prompt is accepted anyway.
 *
 * Past this point a prompt that may still be repainting beats no answer.
 */
export const DEFAULT_PROMPT_IDLE_GRACE_RATIO = 0.8;

/** Prompt type assumed when the caller does not say. */
export const DEFAULT_INPUT_TYPE = "multi_key";

/** Seconds to pause after sending. */
export const DEFAULT_WAIT_AFTER_SEC = 0.2;

/** The session surface prompt waiting and input need. */
export interface PromptSession {
  /** Write to the far end. */
  send(data: string): Promise<void>;
  /** The current screen and any prompt detected on it. */
  snapshot?(): Record<string, unknown>;
  /** Resolve when the screen changes, or on timeout. */
  waitForUpdate?(options: { timeoutMs: number; since?: number }): Promise<boolean>;
  /** Whether the transport is up. Absent means assume it is. */
  isConnected?: boolean | (() => boolean | Promise<boolean>);
  /** How long the session expects to keep repainting. */
  secondsUntilIdle?: (() => number) | undefined;
}

/** A prompt the waiter accepted. */
export interface DetectedPrompt {
  /** The screen the prompt was found on. */
  screen: string;
  /** Which prompt it is, or empty when the detector could not say. */
  promptId: string;
  /** What kind of answer it wants. */
  inputType?: unknown;
  /** Any fields the detector parsed out of the screen. */
  kvData?: unknown;
  /** Whether the screen had settled. */
  isIdle: boolean;
}

/** Options for {@link sendInput}. */
export interface SendInputOptions {
  /** What the prompt is asking for. */
  inputType?: string;
  /** Seconds to pause afterwards, letting the far end react. */
  waitAfterSec?: number;
  /** How the pause is taken. Injected so a test need not spend real time. */
  sleep?: (seconds: number) => Promise<void>;
}

/** Options for {@link PromptWaiter.waitForPrompt}. */
export interface WaitForPromptOptions {
  /** Only accept a prompt whose id contains this. */
  expectedPromptId?: string;
  /** How long to wait overall. */
  timeoutMs?: number;
  /** Polling backstop between screen updates. */
  readIntervalMs?: number;
  /** Return false to reject a candidate. */
  onPromptDetected?: (detected: Record<string, unknown>) => boolean;
  /** Called for every candidate, accepted or not. */
  onPromptSeen?: (detected: Record<string, unknown>) => void;
  /** Called with why a candidate was rejected. */
  onPromptRejected?: (detected: Record<string, unknown>, reason: string) => void;
  /** Whether the screen must have settled. */
  requireIdle?: boolean;
  /** Fraction of the timeout after which idleness stops being required. */
  idleGraceRatio?: number;
}

/** Whether the session reports itself connected. */
async function isConnected(session: PromptSession): Promise<boolean> {
  const checker = session.isConnected;
  if (checker === undefined) {
    // Absence of the check is not evidence of disconnection.
    return true;
  }
  // Called on the session rather than through a detached reference: Python's
  // getattr hands back a bound method, and reading the property here does
  // not, so an implementation using `this` would break.
  return Boolean(typeof checker === "function" ? await checker.call(session) : checker);
}

/** Throw unless the session is present and up. */
async function assertConnected(session: PromptSession | undefined): Promise<PromptSession> {
  if (session === undefined) {
    throw new Error("Session is None");
  }
  if (!(await isConnected(session))) {
    // Better a clear error than keystrokes dropped into a closed socket.
    throw new Error("Session disconnected");
  }
  return session;
}

/** Pause for `seconds`. */
function sleep(seconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

/**
 * Send keystrokes, respecting what the prompt is asking for.
 *
 * The three forms differ in ways that fail quietly rather than loudly.
 * `single_key` sends the keys bare — appending a return to a menu that wanted
 * one keypress submits an extra blank line, which most menus read as "do that
 * again". `any_key` sends a space and ignores the caller's text entirely,
 * because "press any key" wants a keypress. Anything unrecognised is treated
 * as `multi_key` and gets a return, so a typo in the prompt type still
 * submits rather than hanging.
 *
 * The return is appended unconditionally: guessing whether the caller already
 * meant to submit would make the behaviour depend on their input's last
 * character.
 */
export async function sendInput(
  session: PromptSession | undefined,
  keys: string,
  options: SendInputOptions = {},
): Promise<void> {
  const live = await assertConnected(session);
  const inputType = options.inputType ?? DEFAULT_INPUT_TYPE;
  const waitAfterSec = options.waitAfterSec ?? DEFAULT_WAIT_AFTER_SEC;

  if (inputType === "single_key") {
    await live.send(keys);
  } else if (inputType === "any_key") {
    await live.send(" ");
  } else {
    await live.send(`${keys}\r`);
  }

  // Strictly greater than zero: a pause of nothing is not a pause, and
  // scheduling one would still cost the caller a turn of the event loop.
  if (waitAfterSec > 0) {
    await (options.sleep ?? sleep)(waitAfterSec);
  }
}

/** Waits for a prompt to appear in a session's snapshots. */
export class PromptWaiter {
  readonly #session: PromptSession | undefined;
  readonly #onScreenUpdate: ((screen: string) => void) | undefined;

  constructor(session?: PromptSession, onScreenUpdate?: (screen: string) => void) {
    this.#session = session;
    this.#onScreenUpdate = onScreenUpdate;
  }

  /**
   * Poll until a matching prompt is detected.
   *
   * @throws {Error} When the session is absent or disconnected, or when the
   *   timeout passes without a match.
   */
  async waitForPrompt(options: WaitForPromptOptions = {}): Promise<DetectedPrompt> {
    const timeoutMs = options.timeoutMs ?? DEFAULT_PROMPT_TIMEOUT_MS;
    const readIntervalMs = options.readIntervalMs ?? DEFAULT_PROMPT_READ_INTERVAL_MS;
    const requireIdle = options.requireIdle ?? DEFAULT_PROMPT_REQUIRE_IDLE;
    const idleGraceRatio = options.idleGraceRatio ?? DEFAULT_PROMPT_IDLE_GRACE_RATIO;

    const start = performance.now();
    const timeoutSec = timeoutMs / 1000;
    const readIntervalSec = readIntervalMs / 1000;

    while ((performance.now() - start) / 1000 < timeoutSec) {
      const session = await assertConnected(this.#session);
      const snapshot = session.snapshot?.() ?? {};
      const screen = String(snapshot["screen"] ?? "");
      this.#onScreenUpdate?.(screen);

      if (!("prompt_detected" in snapshot)) {
        const remaining = Math.max(0, timeoutSec - (performance.now() - start) / 1000);
        // Never past the deadline, however long the backstop interval is.
        await session.waitForUpdate?.({ timeoutMs: Math.trunc(Math.min(readIntervalSec, remaining) * 1000) });
        continue;
      }

      const detected = (snapshot["prompt_detected"] ?? {}) as Record<string, unknown>;
      const full: Record<string, unknown> = {
        ...detected,
        screen,
        screen_hash: snapshot["screen_hash"] ?? "",
        captured_at: snapshot["captured_at"],
      };
      const promptId = String(detected["prompt_id"] ?? "");
      const isIdle = Boolean(detected["is_idle"] ?? false);
      options.onPromptSeen?.(full);

      const elapsed = (performance.now() - start) / 1000;
      if (
        await this.#waitIfNotIdle(
          session,
          full,
          isIdle,
          elapsed,
          timeoutSec,
          idleGraceRatio,
          readIntervalSec,
          requireIdle,
          options,
        )
      ) {
        continue;
      }
      if (await this.#rejectedByFilters(session, full, promptId, readIntervalSec, options)) {
        continue;
      }

      return {
        screen,
        promptId,
        inputType: detected["input_type"],
        kvData: full["kv_data"],
        isIdle,
      };
    }
    throw new Error(`No prompt detected within ${timeoutMs}ms`);
  }

  /**
   * Wait out an unsettled screen, unless the grace period has passed.
   *
   * Returns whether this candidate was skipped. The session usually knows
   * better than a fixed interval how long it will keep repainting, so it is
   * asked first.
   */
  async #waitIfNotIdle(
    session: PromptSession,
    full: Record<string, unknown>,
    isIdle: boolean,
    elapsed: number,
    timeoutSec: number,
    idleGraceRatio: number,
    readIntervalSec: number,
    requireIdle: boolean,
    options: WaitForPromptOptions,
  ): Promise<boolean> {
    if (!(requireIdle && !isIdle && elapsed < timeoutSec * idleGraceRatio)) {
      return false;
    }
    options.onPromptRejected?.(full, "not_idle");
    const remainingIdle = session.secondsUntilIdle?.() ?? readIntervalSec;
    const waitMs = Math.trunc(Math.max(1, Math.min(remainingIdle, timeoutSec - elapsed) * 1000));
    await session.waitForUpdate?.({ timeoutMs: waitMs });
    return true;
  }

  /**
   * Apply the caller's filters.
   *
   * Returns whether this candidate was rejected. The reason is reported
   * because a caller watching a wait fail otherwise cannot tell a filter from
   * a mismatch from an unsettled screen.
   */
  async #rejectedByFilters(
    session: PromptSession,
    full: Record<string, unknown>,
    promptId: string,
    readIntervalSec: number,
    options: WaitForPromptOptions,
  ): Promise<boolean> {
    const waitMs = Math.trunc(readIntervalSec * 1000);
    // Substring rather than equality, so a caller can wait for "menu"
    // without knowing the full identifier.
    if (
      options.expectedPromptId !== undefined &&
      options.expectedPromptId !== "" &&
      !promptId.includes(options.expectedPromptId)
    ) {
      options.onPromptRejected?.(full, "expected_mismatch");
      await session.waitForUpdate?.({ timeoutMs: waitMs });
      return true;
    }
    if (options.onPromptDetected !== undefined && !options.onPromptDetected(full)) {
      options.onPromptRejected?.(full, "callback_reject");
      await session.waitForUpdate?.({ timeoutMs: waitMs });
      return true;
    }
    return false;
  }
}
