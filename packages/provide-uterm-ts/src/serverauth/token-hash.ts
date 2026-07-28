//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * At-rest storage for tunnel bearer tokens.
 *
 * Port of `provide.uterm.tunnel.token_hash`.
 *
 * The hub holds tunnel tokens in memory. A disclosure — a core dump, a
 * debugger, a log of object state — would otherwise leak every active share
 * and control token verbatim; hashing before storage means it leaks only
 * digests, and the originals cannot be reconstructed without brute-forcing a
 * 256-bit preimage.
 *
 * The digest is BLAKE2b-256, which the host runtime cannot produce: it offers
 * only the 64-byte variant, and the length is part of the hash rather than a
 * truncation of it. See `pycompat/blake2b`.
 */

import { timingSafeEqual } from "node:crypto";
import { blake2b } from "../pycompat/index.ts";

/** Matching the entropy of the 32-byte tokens the tunnel routes issue. */
const DIGEST_BYTES = 32;

/**
 * The digest of a token.
 *
 * An empty token hashes to the empty string rather than to the digest of
 * nothing, so that "no token configured" reads the same as "no match" — a
 * digest of the empty string would authenticate a caller who sent nothing.
 */
export function hashToken(plain: string): string {
  if (plain === "") {
    return "";
  }
  return Buffer.from(blake2b(Buffer.from(plain, "utf8"), DIGEST_BYTES)).toString("hex");
}

/**
 * Whether a token matches a stored digest.
 *
 * Constant-time against the digest, which is what stops an attacker learning
 * a stored hash a byte at a time. Both an empty token and an empty stored
 * hash refuse: a configured-but-empty slot must never authenticate anyone.
 */
export function verifyToken(plain: string, storedHash: string): boolean {
  // Neither half of this test can change an answer on its own: an empty token
  // hashes to the empty string and an empty stored hash has no length, so the
  // length check below refuses both anyway. Stated here because "an empty
  // slot authenticates nobody" is the rule, and leaving it to be inferred
  // from a length comparison two lines down is not stating it.
  if (plain === "" || storedHash === "") {
    return false;
  }
  const candidate = Buffer.from(hashToken(plain), "utf8");
  const stored = Buffer.from(storedHash, "utf8");
  // A length mismatch cannot be compared in constant time, and a stored value
  // of the wrong length is not a weaker check — it is no check.
  if (candidate.length !== stored.length) {
    return false;
  }
  // Constant-time. No assertion can see the difference — the answer is the
  // same either way — which is exactly why it is worth naming: a reviewer
  // swapping this for an ordinary comparison would break nothing a test
  // could catch.
  return timingSafeEqual(candidate, stored);
}
