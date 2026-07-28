//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Encoding helpers shared by the hub's worker-bound paths.
 *
 * Port of the Python module `provide.uterm.server.bridge.hub.core_helpers`
 * and the Go package `hub` (`frames_encode.go`).
 */

import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";

/** Wall and monotonic clocks, injected so tests need not depend on real time. */
export interface Clocks {
  /** Seconds since the epoch. */
  wall(): number;
  /** Seconds from a clock that cannot jump backwards. */
  monotonic(): number;
}

/** The real clocks. */
const systemClocks: Clocks = {
  wall: () => Date.now() / 1000,
  monotonic: () => performance.now() / 1000,
};

/**
 * Encode a message for the worker socket.
 *
 * An `input` message goes out as raw terminal data; everything else is
 * DLE/STX-framed control JSON. That dispatch is not cosmetic — sending a
 * control frame down the terminal path would feed JSON straight to the PTY.
 *
 * A missing type, a null one, an empty string and a non-string all count as
 * *not* input, matching the reference's `str(msg.get("type") or "")`, which
 * folds them together before comparing.
 *
 * The coercion is faithful rather than load-bearing: no value that survives
 * JSON decoding stringifies to `"input"` without already being that string,
 * so dropping it would not change any reachable outcome. It stays because the
 * reference has it, not because a test can tell the difference.
 */
export function encodeWorkerFrame(message: Record<string, unknown>): string {
  const type = message.type;
  if (String(type ?? "") === "input") {
    return encodeTerminalData(String(message.data ?? ""));
  }
  return encodeControlFrame(message);
}

/**
 * Convert a monotonic timestamp to wall-clock seconds.
 *
 * Timestamps are held monotonically so a system clock adjustment cannot make
 * a lease look renewed or long dead; external consumers want wall time, so
 * the offset between the two clocks is applied at the boundary.
 *
 * An absent timestamp passes through rather than converting to "now".
 */
export function monoToWall(monoTs?: number, clocks: Clocks = systemClocks): number | undefined {
  if (monoTs === undefined) {
    return undefined;
  }
  return clocks.wall() + (monoTs - clocks.monotonic());
}
