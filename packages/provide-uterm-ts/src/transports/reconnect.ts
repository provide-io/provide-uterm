//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Surviving a transport drop.
 *
 * Port of the Python module `provide.uterm.transports.reconnect`.
 *
 * A transport that drops mid-session should come back without the caller
 * noticing — but the retry budget is what stops "come back" turning into a
 * client hammering a server that is already down.
 */

/** Retry budget and backoff. */
export interface ReconnectPolicy {
  /** How many retries before giving up. */
  maxRetries?: number;
  /** First delay, doubled each attempt. */
  baseBackoffS?: number;
  /** Ceiling the delay saturates at. */
  maxBackoffS?: number;
}

/** Reference defaults. */
export const RECONNECT_DEFAULTS = {
  maxRetries: 5,
  baseBackoffS: 0.5,
  maxBackoffS: 30,
} as const satisfies { maxRetries: number; baseBackoffS: number; maxBackoffS: number };

/** Error codes and names that mean the transport went, not the caller erred. */
const RETRYABLE = new Set([
  "ECONNRESET",
  "ECONNREFUSED",
  "ECONNABORTED",
  "EPIPE",
  "ETIMEDOUT",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "ENOTCONN",
]);

/** Error names that mean the far end closed. */
const RETRYABLE_NAMES = new Set(["ConnectionClosed", "ConnectionError", "AbortError"]);

/** Options for {@link connectWithRetries} and {@link reconnecting}. */
export interface ReconnectOptions<T> {
  /** Retry budget and backoff. */
  policy?: ReconnectPolicy;
  /** How the backoff is taken. Injected so a test need not spend real time. */
  sleep?: (seconds: number) => Promise<void>;
  /** Re-establish application state on the new session before retrying. */
  onReconnect?: (session: T) => Promise<void>;
}

/** Wait for `seconds`. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

/**
 * How long to wait before a one-based attempt number.
 *
 * Exponential and bounded: a long outage settles at a steady rate rather than
 * drifting towards never retrying. The exponent is clamped at zero, so
 * attempt zero costs the base delay rather than half of it.
 */
export function policyDelay(policy: ReconnectPolicy, attempt: number): number {
  const base = policy.baseBackoffS ?? RECONNECT_DEFAULTS.baseBackoffS;
  const ceiling = policy.maxBackoffS ?? RECONNECT_DEFAULTS.maxBackoffS;
  return Math.min(base * 2 ** Math.max(attempt - 1, 0), ceiling);
}

/**
 * Whether a failure is worth retrying.
 *
 * A connection fault is; a programming error is not — retrying one only
 * delays the report by the whole budget, and the second attempt fails exactly
 * as the first did.
 */
export function isRetryableTransportError(error: unknown): boolean {
  if (!(error instanceof Error)) {
    return false;
  }
  const code = (error as { code?: unknown }).code;
  if (typeof code === "string" && RETRYABLE.has(code)) {
    return true;
  }
  if (RETRYABLE_NAMES.has(error.name)) {
    return true;
  }
  // Node surfaces some of these only in the message, and a transport wrapper
  // may re-throw with the code folded in.
  return [...RETRYABLE].some((known) => error.message.includes(known));
}

/**
 * Connect, retrying within the policy's budget.
 *
 * @throws {Error} `connect retries exhausted`, carrying the last real failure
 *   as its cause — the message alone tells an operator nothing about why.
 */
export async function connectWithRetries<T>(connect: () => Promise<T>, options: ReconnectOptions<T> = {}): Promise<T> {
  const policy = options.policy ?? {};
  const maxRetries = policy.maxRetries ?? RECONNECT_DEFAULTS.maxRetries;
  const sleep = options.sleep ?? realSleep;
  let retries = 0;
  for (;;) {
    try {
      return await connect();
    } catch (error) {
      if (retries >= maxRetries) {
        throw new Error("connect retries exhausted", { cause: error });
      }
      retries += 1;
      const delay = policyDelay(policy, retries);
      if (delay > 0) {
        await sleep(delay);
      }
    }
  }
}

/** A session that rebuilds itself when the transport drops. */
export interface Reconnecting<T> {
  /** The live session, once one has been established. */
  readonly session: T | undefined;
  /** Run an operation, reconnecting and retrying if the transport drops. */
  run<R>(operation: (session: T) => Promise<R>): Promise<R>;
}

/**
 * Wrap a connect function so operations survive a transport drop.
 *
 * Each retry runs against the *newly connected* session — retrying against
 * the dead one would fail the same way forever. The reconnect hook runs
 * first, so application state is back before the retried call needs it.
 */
export function reconnecting<T>(connect: () => Promise<T>, options: ReconnectOptions<T> = {}): Reconnecting<T> {
  const policy = options.policy ?? {};
  const maxRetries = policy.maxRetries ?? RECONNECT_DEFAULTS.maxRetries;
  const sleep = options.sleep ?? realSleep;
  let session: T | undefined;

  /** Establish a session, or reuse the one already open. */
  const ensure = async (): Promise<T> => {
    if (session === undefined) {
      session = await connectWithRetries(connect, options);
    }
    return session;
  };

  /** Drop the current session and build another. */
  const rebuild = async (attempt: number): Promise<T> => {
    session = undefined;
    const delay = policyDelay(policy, attempt);
    if (delay > 0) {
      await sleep(delay);
    }
    const fresh = await connectWithRetries(connect, options);
    session = fresh;
    await options.onReconnect?.(fresh);
    return fresh;
  };

  return {
    get session(): T | undefined {
      return session;
    },
    async run<R>(operation: (session: T) => Promise<R>): Promise<R> {
      let current = await ensure();
      let retries = 0;
      for (;;) {
        try {
          return await operation(current);
        } catch (error) {
          if (!isRetryableTransportError(error)) {
            throw error;
          }
          if (retries >= maxRetries) {
            throw new Error("reconnect retries exhausted", { cause: error });
          }
          retries += 1;
          current = await rebuild(retries);
        }
      }
    },
  };
}
