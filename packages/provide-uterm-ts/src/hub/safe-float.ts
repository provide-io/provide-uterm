//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Coercing a number off the wire.
 *
 * Port of `_safe_float` from `provide.uterm.server.bridge.models`.
 *
 * Values arriving in a frame are whatever a client sent, and a bad number must
 * not take the connection down with it. Each of these becomes a timeout, a
 * rate or an interval, so the fallback is what the hub was configured with
 * rather than zero.
 */

import { pyFloat } from "../pycompat/index.ts";

/**
 * Read a number, or fall back.
 *
 * An absent value and an unreadable one are not distinguished: a caller that
 * omitted a field and one that sent nonsense both want whatever the server
 * would have used.
 *
 * The parse is CPython's `float`, not the host's `Number` — the two disagree
 * on values a client can actually send. `Number("")` is zero where `float("")`
 * raises; `Number("0x10")` is sixteen where `float("0x10")` raises; and
 * `float` takes `inf` and `nan` by name where `Number` does not.
 */
export function safeFloat(value: unknown, fallback: number): number {
  if (typeof value === "number") {
    return value;
  }
  // `float(True)` is one, which a client sending a boolean for a rate
  // therefore gets — the reference does not refuse it, so neither does this.
  if (typeof value === "boolean") {
    return value ? 1 : 0;
  }
  // Everything else that is not text — a list, an object, nothing at all —
  // has no numeric reading, and the caller wanted a number.
  if (typeof value !== "string") {
    return fallback;
  }
  return pyFloat(value) ?? fallback;
}
