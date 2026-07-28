//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Reading the protocol range off a worker's first frame.
 *
 * Port of the range-reading half of `_handle_worker_hello` in
 * `provide.uterm.server.bridge.routes.websockets_worker`, where the reference
 * reads it inline in the receive loop.
 *
 * Three shapes have to be understood at once: the current range object, the
 * legacy single version, and a worker that says nothing at all. Nothing here
 * decides whether the two can talk — {@link negotiateProtocolVersion} does
 * that — this only says what the worker claimed.
 */

import { safeInt } from "../pycompat/index.ts";
import { MAX_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION } from "./contracts.ts";

/** The lowest version anything may claim. */
const LOWEST_VERSION = 1;

/** What a worker says it can speak. */
export interface ProtocolRange {
  min: number;
  max: number;
}

/**
 * Read the versions a hello advertises.
 *
 * A worker that advertises nothing speaks version one: that is what every
 * client did before the field existed, and refusing them would disconnect
 * every worker built against an older hub.
 *
 * A legacy single version is a range of exactly itself, not a minimum with an
 * open top — a worker that can only speak version one must not be handed
 * version two because the hub happens to support it.
 *
 * A version that cannot be read falls back rather than failing. The same
 * frame carries the input mode, so dropping the connection here would lose
 * that too; the negotiation that follows is what refuses a worker the hub
 * cannot talk to.
 */
export function readClientProtocolRange(hello: Record<string, unknown>): ProtocolRange {
  const block = hello.protocol;
  // Only an object is a range. A worker that sent something else has not sent
  // one, and whatever else it said still applies. The array test cannot change
  // an answer — an array has no `min` or `max`, so reading one as a range
  // yields the same defaults as skipping it — and is kept because "a list is
  // not a range" is the rule, not an accident of what arrays lack.
  if (typeof block === "object" && block !== null && !Array.isArray(block)) {
    const range = block as Record<string, unknown>;
    return {
      // Each bound defaults separately, from what the hub itself speaks: a
      // range naming only one end still has the other. The two constants are
      // equal while the hub speaks one version, so taking the wrong one is
      // invisible today — a test asserts they are equal, and fails the day
      // that stops being true.
      min: safeInt(range.min, MIN_PROTOCOL_VERSION, { minVal: LOWEST_VERSION }),
      max: safeInt(range.max, MAX_PROTOCOL_VERSION, { minVal: LOWEST_VERSION }),
    };
  }
  if ("protocol_version" in hello) {
    // Read by presence rather than by value, mirroring the reference's `in`.
    // Both reach the same answer for a field that is present but undefined,
    // since an unreadable version falls back to the floor either way.
    const legacy = safeInt(hello.protocol_version, 0);
    // Zero and negatives are not versions; taking them literally would put
    // the negotiated floor below anything the hub ever spoke. The fallback of
    // zero is the reference's; any value below the floor is raised to it, so
    // the two steps together admit nothing lower whatever the fallback was.
    const version = legacy >= LOWEST_VERSION ? legacy : LOWEST_VERSION;
    return { min: version, max: version };
  }
  return { min: LOWEST_VERSION, max: LOWEST_VERSION };
}
