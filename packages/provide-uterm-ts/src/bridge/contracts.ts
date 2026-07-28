//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The contract shared between the server, the Cloudflare adapter and the
 * browser.
 *
 * Port of the Python module `provide.uterm.bridge.contracts`.
 *
 * The response shapes themselves live in `../frames`, generated from the
 * Pydantic models that are the single source of truth. What remains here is
 * the part with behaviour: the handshake, and the literal vocabularies both
 * sides have to agree on.
 */

/**
 * Oldest protocol version this build still speaks.
 *
 * Kept at the oldest version still supported when the maximum moves, so a
 * peer that has not been redeployed can still connect.
 */
export const MIN_PROTOCOL_VERSION = 1;

/** Newest protocol version this build speaks. */
export const MAX_PROTOCOL_VERSION = 1;

/** The version to pick when the choice is free. */
export const PREFERRED_PROTOCOL_VERSION = 1;

/**
 * Alias for stamping outbound frames.
 *
 * Tied to the preferred version rather than set separately: if the two
 * drifted, a peer would be told one version during the handshake and sent
 * frames labelled with another.
 */
export const CURRENT_PROTOCOL_VERSION = PREFERRED_PROTOCOL_VERSION;

/** Lifecycle states a session moves through. */
export const SESSION_LIFECYCLES = ["stopped", "starting", "running", "error"] as const;

/** One of {@link SESSION_LIFECYCLES}. */
export type SessionLifecycle = (typeof SESSION_LIFECYCLES)[number];

/** Whether input is gated behind a lease or open to operators. */
export const INPUT_MODES = ["hijack", "open"] as const;

/** One of {@link INPUT_MODES}. */
export type ContractInputMode = (typeof INPUT_MODES)[number];

/** Who may see a session. */
export const VISIBILITIES = ["public", "operator", "private"] as const;

/** One of {@link VISIBILITIES}. */
export type Visibility = (typeof VISIBILITIES)[number];

/**
 * The wire vocabulary.
 *
 * Shared with the Cloudflare adapter and the browser; a type missing here is
 * a frame nobody can route.
 */
export const FRAME_TYPES = [
  "snapshot_req",
  "snapshot",
  "term",
  "input",
  "control",
  "hijack_state",
  "analysis",
  "error",
  "worker_connected",
  "worker_disconnected",
  "worker_hello",
  "heartbeat",
  "ping",
  "hijack_request",
  "hijack_release",
  "hijack_step",
  "hello",
  "resume",
] as const;

/** One of {@link FRAME_TYPES}. */
export type FrameType = (typeof FRAME_TYPES)[number];

/**
 * The version both sides should use, or nothing if they cannot agree.
 *
 * The highest of the two ranges' intersection — not the client's preference
 * and not the server's. Picking lower would silently downgrade a pair that
 * could have spoken something newer.
 *
 * Nothing means the handshake must fail and the caller should close 1002.
 * Letting it proceed instead would leave two peers disagreeing about the
 * wire format, which surfaces later as corrupt frames rather than as a clean
 * disconnect. A reversed range — a confused client sending a maximum below
 * its minimum — is an empty intersection and refused for the same reason.
 *
 * Bounds are truncated toward zero, matching the reference's `int()`: a
 * client advertising 1.9 supports 1, not 2.
 *
 * While the server's range is a single version — the current lockstep, where
 * MIN and MAX are both 1 — the intersection can only ever be one point. Two
 * details are therefore untestable today and will become live the moment MAX
 * moves: that the *highest* of the intersection is chosen rather than the
 * lowest, and that the client's maximum is truncated. Both are written the
 * way the reference writes them so that widening the range is a one-line
 * change rather than a bug hunt.
 */
export function negotiateProtocolVersion(clientMin: number, clientMax: number): number | undefined {
  const low = Math.max(Math.trunc(clientMin), MIN_PROTOCOL_VERSION);
  const high = Math.min(Math.trunc(clientMax), MAX_PROTOCOL_VERSION);
  return low > high ? undefined : high;
}
