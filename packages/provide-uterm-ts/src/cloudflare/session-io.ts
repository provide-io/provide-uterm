//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a session runtime accepts from a browser, and what it tells one back.
 *
 * Port of the request and hijack-state halves of
 * `provide.uterm.cloudflare.do.session_runtime.io`.
 *
 * The content-type rule is a CSRF guard rather than a formality. A browser can
 * send `text/plain`, `multipart/form-data` or a form encoding to another
 * origin *without* a preflight, so a handler that parsed any of those would
 * take instructions from whatever page a session's owner happened to have
 * open. Requiring `application/json` means the request has to survive a
 * preflight first, and a cross-origin page cannot get one.
 *
 * The size cap is the other half: a Durable Object has little memory, and a
 * body it has already read is memory it has already spent.
 */

/** How much of a request body will be read before it is refused. */
export const MAX_REQUEST_BODY = 65_536;

/**
 * How many webhook deliveries may be in flight at once.
 *
 * Delivery is off the broadcast path so a slow endpoint cannot stall the frame
 * loop; this is what stops the backlog growing without bound instead.
 */
export const MAX_INFLIGHT_WEBHOOKS = 64;

/** What a browser is told about the hijack. */
export interface HijackStateFrame {
  type: "hijack_state";
  hijacked: boolean;
  /** `me` for the browser holding it, `other` for everyone else, null for none. */
  owner: "me" | "other" | null;
  /** Wall-clock, so a browser can count down against its own clock. */
  lease_expires_at: number | null;
  input_mode: string;
  ts: number;
}

/** What the runtime knows when it answers a browser. */
export interface HijackStateInput {
  /** The live hijack, or nothing when nobody holds one. */
  session?: { hijackId: string; leaseExpiresAt: number | null } | undefined;
  /** The hijack this particular browser is recorded as owning, if any. */
  browserHijackId?: string | undefined;
  inputMode: string;
  now: number;
  /** The runtime's monotonic clock, for converting the expiry. */
  monotonic: number;
}

/**
 * Read a request body as an object, or refuse it.
 *
 * **A recorded divergence.** The reference returns an empty object for every
 * other kind of bad input — wrong content type, oversized, empty, not an
 * object — but a body that is not valid JSON raises out of it. Whether that
 * surfaces as a 500 depends on a handler above it that this port has not
 * traced; what is certain is that the function is inconsistent with itself.
 * This returns an empty object for that case too.
 *
 * @returns The object the body carried, or an empty one for anything else.
 */
export function requestJson(contentType: string | undefined, body: string): Record<string, unknown> {
  // Matched as a substring, as the reference does. That is not the hole it
  // looks like: a cross-origin request can only set the three types that skip
  // a preflight, and none of them contains this one.
  if (
    !String(contentType ?? "")
      .toLowerCase()
      .includes("application/json")
  ) {
    return {};
  }
  // No separate empty-body check: the reference needs one because it lets a
  // parse failure escape, and this does not.
  // Checked against the string that was already read, as the reference checks
  // it — the read itself is the runtime's to bound.
  if (body.length > MAX_REQUEST_BODY) {
    return {};
  }
  let value: unknown;
  try {
    value = JSON.parse(body);
  } catch {
    return {};
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}

/**
 * A monotonic instant as a wall-clock one.
 *
 * A lease is monotonic in memory — so a countdown survives a clock adjustment
 * — and wall-clock everywhere it is *reported*, because a browser has no
 * access to this process's monotonic clock and cannot count against it.
 */
export function monoToWall(mono: number | null | undefined, now: number, monotonic: number): number | null {
  if (mono === null || mono === undefined) {
    return null;
  }
  return mono + (now - monotonic);
}

/**
 * A wall-clock instant as a monotonic one.
 *
 * The other direction, used when a persisted lease is read back: it was stored
 * as wall-clock so it would survive a restart, and has to become monotonic
 * again before anything counts down against it.
 */
export function wallToMono(wall: number, now: number, monotonic: number): number {
  return wall - (now - monotonic);
}

/**
 * What one browser is told about the hijack.
 *
 * `owner` is `me` only when this browser's recorded hijack id matches the live
 * session's. A browser with no recorded id, or a stale one from a hijack that
 * has since ended, is told `other` — which is the safe answer, because being
 * wrongly told `me` is what would put another operator's controls in front of
 * somebody.
 */
export function hijackStateFrame(state: HijackStateInput): HijackStateFrame {
  const session = state.session;
  return {
    type: "hijack_state",
    hijacked: session !== undefined,
    owner: session === undefined ? null : state.browserHijackId === session.hijackId ? "me" : "other",
    // No session means no expiry, which falls out of the conversion itself.
    lease_expires_at: monoToWall(session?.leaseExpiresAt, state.now, state.monotonic),
    input_mode: state.inputMode,
    ts: state.now,
  };
}

/** One browser the runtime is broadcasting to. */
export interface BroadcastTarget {
  wsId: string;
  /** The hijack this browser is recorded as owning, if any. */
  hijackId?: string | undefined;
}

/** What a broadcast produced, and who is left afterwards. */
export interface BroadcastResult {
  sends: Array<{ wsId: string; frame: HijackStateFrame }>;
  /** Sockets that failed and have been forgotten, in the order they failed. */
  dropped: string[];
}

/**
 * Tell every browser where the hijack stands.
 *
 * A socket that fails is forgotten — both the socket and its recorded hijack
 * ownership — rather than retried: it has already gone, and an entry left
 * behind would let a hijack look owned by nobody reachable.
 *
 * @param send Delivers one frame. Throwing means the socket has gone.
 */
export async function broadcastHijackState(
  targets: readonly BroadcastTarget[],
  state: Omit<HijackStateInput, "browserHijackId">,
  send: (wsId: string, frame: HijackStateFrame) => Promise<void>,
): Promise<BroadcastResult> {
  const result: BroadcastResult = { sends: [], dropped: [] };
  for (const target of targets) {
    const frame = hijackStateFrame({ ...state, browserHijackId: target.hijackId });
    try {
      await send(target.wsId, frame);
      result.sends.push({ wsId: target.wsId, frame });
    } catch {
      result.dropped.push(target.wsId);
    }
  }
  return result;
}
