//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * How a session frames what it sends.
 *
 * Port of `send_ws` from
 * `provide.uterm.cloudflare.do.session_runtime.ws_helpers`.
 *
 * One socket carries two kinds of thing — terminal bytes and control frames —
 * and which a payload becomes is decided by its type alone.
 */

import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";

/**
 * The two types that are terminal data.
 *
 * Everything else is a control frame, which is the safe direction: a control
 * frame a browser does not understand is ignored, where terminal bytes it did
 * not expect are printed to the screen.
 */
const TERMINAL_TYPES: ReadonlySet<string> = new Set(["input", "term"]);

/**
 * Render a terminal payload's data as the reference does.
 *
 * An absent field is the empty string; anything else is stringified rather
 * than refused, because a frame that failed to send would lose the session's
 * output rather than one field of it.
 */
function stringifyData(data: unknown): string {
  return data === undefined ? "" : String(data);
}

/** Whether a payload would be framed as terminal data. */
export function isTerminalFrame(payload: Record<string, unknown>): boolean {
  return typeof payload.type === "string" && TERMINAL_TYPES.has(payload.type);
}

/**
 * Frame a payload for the wire.
 *
 * Terminal data goes out as terminal data rather than as a control frame
 * carrying it: a browser reads the two differently, and a screen update
 * delivered as a control frame would render as nothing at all.
 *
 * The type is compared exactly — `TERM` and `" term "` are not it. A payload
 * whose type is absent, empty or not a string is a control frame.
 */
export function frameForWire(payload: Record<string, unknown>): string {
  if (isTerminalFrame(payload)) {
    return encodeTerminalData(stringifyData(payload.data));
  }
  return encodeControlFrame(payload);
}
