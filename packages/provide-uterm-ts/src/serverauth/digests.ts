//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Constant-time digest comparison.
 *
 * Shared by the webhook signature check and the API key store: both compare a
 * value supplied by whoever is asking against one this server computed, and
 * an early exit on the first differing byte tells the caller how much of
 * their guess was right.
 */

import { timingSafeEqual } from "node:crypto";

/** Whether two hex digests are the same, without leaking where they differ. */
export function digestsMatch(supplied: string, expected: string): boolean {
  const left = Buffer.from(supplied, "utf8");
  const right = Buffer.from(expected, "utf8");
  // `timingSafeEqual` refuses different lengths. A length mismatch is already
  // a mismatch, and the length is not the secret.
  return left.length === right.length && timingSafeEqual(left, right);
}
