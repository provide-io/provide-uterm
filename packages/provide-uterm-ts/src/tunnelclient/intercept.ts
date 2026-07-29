//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Pausing an HTTP request so an operator can look at it — and change it.
 *
 * Port of `provide.uterm.tunnel.intercept`. Letting a browser rewrite a
 * request before it is forwarded is the whole feature and the whole danger,
 * so what the rewrite may *not* touch is the part that matters:
 *
 * * **Hop-by-hop headers** are connection-scoped and must not be proxied.
 * * **Framing headers** — a `Content-Length` somebody chose — are how request
 *   smuggling is done against whatever is downstream.
 * * **Identity headers** — `Host`, `Authorization`, `Cookie`, the forwarding
 *   family — would let an operator impersonate the original requester or take
 *   over authentication downstream, which is the very thing interception
 *   exists to make visible.
 *
 * A request that nobody decides about is released the configured way rather
 * than left hanging, and an action nobody defined becomes `forward` — keeping
 * traffic moving rather than silently dropping it.
 */

/** What an operator can decide to do with a paused request. */
export type InterceptAction = "forward" | "drop" | "modify";

/** What was decided. */
export interface InterceptDecision {
  action: InterceptAction;
  /** Replacement headers, already stripped of everything denied. */
  headers: Record<string, string> | undefined;
  /** A replacement body. */
  body: Uint8Array | undefined;
}

/** The actions this understands. Anything else is not one. */
const ACTIONS = new Set<string>(["forward", "drop", "modify"]);

/**
 * Headers an operator's browser may not inject into the forwarded request.
 *
 * Lowercased, since a header named `AUTHORIZATION` is the same header.
 */
export const DENYLISTED_HEADERS: ReadonlySet<string> = new Set([
  // Hop-by-hop: connection-scoped, never proxied.
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  // Framing: a chosen length is how smuggling is done.
  "content-length",
  // Identity and authority: forwarding these is impersonation.
  "host",
  "authorization",
  "cookie",
  "forwarded",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-real-ip",
]);

/** The lowest timeout that can be configured, so a request cannot be released instantly. */
export const MIN_TIMEOUT_S = 1;

/** What a gate does when nobody decides in time. */
export type TimeoutAction = "forward" | "drop";

/** Told what was dropped, so an operator can see why their edit did not take. */
export interface InterceptLogger {
  headersDenied(names: readonly string[]): void;
  invalidBody(id: unknown): void;
}

/** A logger that says nothing. */
const SILENT: InterceptLogger = { headersDenied: () => {}, invalidBody: () => {} };

/** Strip everything denied from an operator-supplied header set. */
export function sanitizeHeaders(
  raw: Readonly<Record<string, string>>,
  logger: InterceptLogger = SILENT,
): Record<string, string> {
  const cleaned: Record<string, string> = {};
  const dropped: string[] = [];
  for (const [key, value] of Object.entries(raw)) {
    if (DENYLISTED_HEADERS.has(key.toLowerCase())) {
      dropped.push(key);
      continue;
    }
    cleaned[key] = value;
  }
  if (dropped.length > 0) {
    logger.headersDenied([...dropped].sort());
  }
  return cleaned;
}

/** A decision that changes nothing. */
function plainDecision(action: InterceptAction): InterceptDecision {
  return { action, headers: undefined, body: undefined };
}

/** Whether a string is valid, fully-padded base64 — as CPython's `validate` demands. */
function decodeStrictBase64(value: string): Uint8Array | undefined {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    // A value missing its padding is refused, not guessed at: a body decoded
    // from something the sender did not mean is a body nobody asked for.
    return undefined;
  }
  // No `try` around this: the shape has already been checked, and `Buffer`
  // does not fail on a value that matches it.
  return Uint8Array.from(Buffer.from(value, "base64"));
}

/**
 * Read an operator's decision out of the message that carried it.
 *
 * An action nobody defined becomes `forward`: the choice that keeps traffic
 * moving rather than the one that silently drops it.
 */
export function parseActionMessage(
  message: Record<string, unknown>,
  logger: InterceptLogger = SILENT,
): InterceptDecision {
  const named = message.action === undefined ? "forward" : String(message.action);
  const action = (ACTIONS.has(named) ? named : "forward") as InterceptAction;
  if (action !== "modify") {
    // Headers and a body mean nothing on a decision that is not a rewrite, and
    // reading them would be reading input nobody will use.
    return plainDecision(action);
  }

  let headers: Record<string, string> | undefined;
  const rawHeaders = message.headers;
  if (typeof rawHeaders === "object" && rawHeaders !== null && !Array.isArray(rawHeaders)) {
    headers = sanitizeHeaders(
      Object.fromEntries(Object.entries(rawHeaders).map(([key, value]) => [String(key), String(value)])),
      logger,
    );
  }

  let body: Uint8Array | undefined;
  if (typeof message.body_b64 === "string") {
    body = decodeStrictBase64(message.body_b64);
    if (body === undefined) {
      logger.invalidBody(message.id);
    }
  }

  return { action, headers, body };
}

/** A request paused, waiting for somebody to decide. */
interface Waiting {
  resolve(decision: InterceptDecision): void;
  settled: boolean;
  timer: ReturnType<typeof setTimeout> | undefined;
}

/** Holds paused requests until they are decided or time out. */
export class InterceptGate {
  /** Whether requests are being paused at all. */
  enabled = false;
  /** Whether requests are being shown to the operator. */
  inspectEnabled = true;
  readonly timeoutS: number;
  readonly timeoutAction: TimeoutAction;

  readonly #pending = new Map<string, Waiting>();

  constructor(timeoutS = 30, timeoutAction = "forward") {
    // Floored, so a gate cannot be configured to release everything the
    // instant it pauses it.
    this.timeoutS = Math.max(MIN_TIMEOUT_S, timeoutS);
    this.timeoutAction = timeoutAction === "drop" ? "drop" : "forward";
  }

  /**
   * How many requests are waiting on somebody.
   *
   * A request stops counting the moment it is decided. The reference keeps
   * counting it until the coroutine awaiting it resumes, which is a window
   * this runtime has no equivalent of; the count here answers "how many are
   * still undecided", which is what it is read as.
   */
  get pendingCount(): number {
    return this.#pending.size;
  }

  /**
   * Wait for a decision about `rid`, or release it when the time runs out.
   *
   * @param sleep How the wait is taken. Injected so a test need not spend it.
   */
  async awaitDecision(rid: string, sleep: (seconds: number) => Promise<void> = realSleep): Promise<InterceptDecision> {
    return new Promise<InterceptDecision>((resolve) => {
      const waiting: Waiting = { resolve, settled: false, timer: undefined };
      this.#pending.set(rid, waiting);

      const settle = (decision: InterceptDecision): void => {
        // Stated, though settling a second time would change nothing on its
        // own: this runtime ignores a second resolution, and the entry has
        // already left the map. It says the intent rather than leaning on
        // both of those staying true.
        if (waiting.settled) {
          return;
        }
        waiting.settled = true;
        this.#pending.delete(rid);
        resolve(decision);
      };
      waiting.resolve = settle;

      void sleep(this.timeoutS).then(() => {
        // Released the configured way rather than left hanging: a paused
        // request nobody answers is a request that never completes.
        settle(plainDecision(this.timeoutAction));
      });
    });
  }

  /**
   * Answer a waiting request.
   *
   * @returns Whether there was one to answer. A second answer for the same
   *   request finds nothing, so the first decision stands.
   */
  resolve(rid: string, decision: InterceptDecision): boolean {
    const waiting = this.#pending.get(rid);
    // Either half is enough while the other stands — a settled request is no
    // longer in the map — and neither is safe to drop alone.
    if (waiting === undefined || waiting.settled) {
      return false;
    }
    waiting.resolve(decision);
    return true;
  }

  /**
   * Release everything waiting.
   *
   * @returns How many were released.
   */
  cancelAll(action: TimeoutAction = "forward"): number {
    const decision = plainDecision(action);
    let count = 0;
    // Everything still here is still waiting: settling removes the entry, so
    // there is no decided-but-listed state to skip. The reference checks for
    // one because its futures stay in the map until the awaiting coroutine
    // resumes — see the note on `pendingCount`.
    for (const waiting of [...this.#pending.values()]) {
      waiting.resolve(decision);
      count += 1;
    }
    // Already empty, since settling each one removed it; cleared so that stays
    // true of any future settling that does not.
    this.#pending.clear();
    return count;
  }
}

/** Wait for `seconds`. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, seconds * 1000);
  });
}
