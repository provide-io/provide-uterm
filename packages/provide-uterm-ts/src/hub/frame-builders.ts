//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Every frame the hub sends a browser.
 *
 * Port of the builders in `provide.uterm.server.bridge.frames`. Built in one
 * place so no route hand-rolls a frame, and so the one thing that varies
 * between them — whether an absent field survives onto the wire — is decided
 * once per frame rather than per call site.
 */

/** The clock a frame is stamped from, replaceable so a frame is testable. */
export type Clock = () => number;

/** Seconds since the epoch, as every timestamp on the wire is. */
const systemClock: Clock = () => Date.now() / 1000;

/** A frame, as it goes out. */
export type Frame = Record<string, unknown>;

/** Stamp a frame with the time it was built, or the time a caller supplied. */
function stamp(ts: number | undefined, clock: Clock): number {
  return ts ?? clock();
}

/** Something went wrong, in words a browser can show. */
export function makeErrorFrame(message: string): Frame {
  return { type: "error", message };
}

/** An answer to a ping. */
export function makePongFrame(ts?: number, clock: Clock = systemClock): Frame {
  return { type: "pong", ts: stamp(ts, clock) };
}

/** A hijack lease has been extended, and until when. */
export function makeHeartbeatAckFrame(leaseExpiresAt: number, ts?: number, clock: Clock = systemClock): Frame {
  return { type: "heartbeat_ack", lease_expires_at: leaseExpiresAt, ts: stamp(ts, clock) };
}

/** A worker has attached to the session. */
export function makeWorkerConnectedFrame(workerId: string, ts?: number, clock: Clock = systemClock): Frame {
  return { type: "worker_connected", worker_id: workerId, ts: stamp(ts, clock) };
}

/** A worker has gone. */
export function makeWorkerDisconnectedFrame(workerId: string, ts?: number, clock: Clock = systemClock): Frame {
  return { type: "worker_disconnected", worker_id: workerId, ts: stamp(ts, clock) };
}

/** Terminal output, on its way to a screen. */
export function makeTermFrame(data: string, ts?: number, clock: Clock = systemClock): Frame {
  return { type: "term", data, ts: stamp(ts, clock) };
}

/**
 * The result of an analysis.
 *
 * A null `raw` survives onto the wire rather than being dropped: the frontend
 * reads the field directly, and an analysis that produced nothing is a
 * different thing from a frame that forgot to say.
 */
export function makeAnalysisFrame(formatted: string, raw: unknown, ts?: number, clock: Clock = systemClock): Frame {
  return { type: "analysis", formatted, raw: raw ?? null, ts: stamp(ts, clock) };
}

/**
 * Who holds the session, if anyone.
 *
 * Both nulls survive, for the same reason: a browser reads `owner` and
 * `lease_expires_at` straight off the frame, so a session with no owner has
 * to say so rather than leave the field out and be read as unchanged.
 */
export function makeHijackStateFrame(
  hijacked: boolean,
  owner: string | undefined,
  leaseExpiresAt: number | undefined,
  inputMode: string,
): Frame {
  return {
    type: "hijack_state",
    hijacked,
    owner: owner ?? null,
    lease_expires_at: leaseExpiresAt ?? null,
    input_mode: inputMode,
  };
}

/**
 * The first frame a browser receives.
 *
 * Carries whatever capability flags the caller adds, which is why it is built
 * from a payload rather than from named arguments — the set grows, and a
 * builder that had to be edited for each one would be edited late.
 *
 * The caller's fields are applied *over* the type, so a caller naming its own
 * `type` wins. Faithful to the reference, and worth knowing rather than
 * discovering.
 */
export function makeHelloFrame(payload: Frame = {}): Frame {
  const withDefaults: Frame = { ...payload };
  // Defaults, not overrides: a caller that says otherwise is answered.
  withDefaults.mcp_supported ??= true;
  withDefaults.vnc_supported ??= true;
  return { type: "hello", ...withDefaults };
}

/**
 * A worker's own status frame, with the gaps filled.
 *
 * The worker composed it, so its fields are kept as they are and only what is
 * missing is supplied — a worker naming its own type is not corrected.
 */
export function coerceWorkerStatusFrame(payload: Frame, clock: Clock = systemClock): Frame {
  const frame: Frame = { ...payload };
  frame.type ??= "status";
  frame.ts ??= clock();
  return frame;
}
