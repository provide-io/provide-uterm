//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Hash-based at-rest storage for tunnel bearer tokens.
 *
 * Port of `provide.uterm.tunnel.token_hash`. The hub holds tunnel tokens in
 * process memory, so a memory disclosure — a core dump, a debugger, a log of
 * object state — would otherwise leak every active share and control token
 * verbatim. Storing the digest means a disclosure leaks digests, which redeem
 * nothing: reconstructing the token means brute-forcing a BLAKE2b preimage of
 * a 256-bit value.
 *
 * Two calls: {@link hashToken} at issuance, {@link verifyToken} at every
 * authentication site.
 */

import { timingSafeEqual } from "node:crypto";
import { blake2b } from "@noble/hashes/blake2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

/**
 * The digest length the reference uses, matching the entropy of the 32-byte
 * token it issues. Larger is wasteful, smaller is weaker.
 */
const DIGEST_BYTES = 32;

/**
 * The BLAKE2b hex digest of `plain`.
 *
 * Empty for an empty token, so a caller can treat "no token configured" the
 * same as "no match".
 *
 * BLAKE2b-256 rather than Node's `createHash`, which offers only
 * BLAKE2b-512 — and truncating that would give a different digest, since the
 * output length is mixed into BLAKE2b's parameter block.
 */
export function hashToken(plain: string): string {
  if (plain === "") {
    return "";
  }
  return bytesToHex(blake2b(new TextEncoder().encode(plain), { dkLen: DIGEST_BYTES }));
}

/**
 * Whether `plain` hashes to `storedHash`, compared in constant time.
 *
 * Both an empty token and an empty stored hash are refused: a
 * configured-but-empty slot must never authenticate any caller.
 */
export function verifyToken(plain: string, storedHash: string): boolean {
  // Either half of this guard is redundant while the other stands — an empty
  // token hashes to nothing and an empty slot holds nothing, so the length
  // check below refuses both anyway. Stated in full because dropping *both*
  // is the hole: two empty strings would then compare equal.
  if (plain === "" || storedHash === "") {
    return false;
  }
  const candidate = Buffer.from(hashToken(plain), "utf8");
  const stored = Buffer.from(storedHash, "utf8");
  // `timingSafeEqual` raises on a length mismatch rather than answering, and
  // a store can hold something the wrong length. The length is not the secret.
  if (candidate.length !== stored.length) {
    return false;
  }
  // The reason this is not `candidate === storedHash`: the two answer alike,
  // and no test can tell them apart. What differs is how long the wrong
  // answer takes.
  return timingSafeEqual(candidate, stored);
}
