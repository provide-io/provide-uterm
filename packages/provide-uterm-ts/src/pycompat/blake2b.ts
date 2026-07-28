//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * BLAKE2b with a configurable digest length.
 *
 * The host runtime offers BLAKE2b only as `blake2b512`, and the digest length
 * is part of the algorithm's parameter block rather than a truncation of its
 * output — so a 32-byte digest is *not* the first half of a 64-byte one. The
 * tunnel token store holds 32-byte digests, read by whichever implementation
 * serves the request, which makes this an interoperability requirement rather
 * than a convenience.
 *
 * RFC 7693. Unkeyed, unsalted, no personalisation: everything this codebase
 * hashes uses the plain construction, and a parameter nobody sets is a
 * parameter nobody can get wrong.
 */

/** The initialisation vector, the fractional parts of the square roots of the first eight primes. */
const IV: readonly bigint[] = [
  0x6a09e667f3bcc908n,
  0xbb67ae8584caa73bn,
  0x3c6ef372fe94f82bn,
  0xa54ff53a5f1d36f1n,
  0x510e527fade682d1n,
  0x9b05688c2b3e6c1fn,
  0x1f83d9abfb41bd6bn,
  0x5be0cd19137e2179n,
];

/** The message-word permutation for each of the twelve rounds. */
const SIGMA: readonly (readonly number[])[] = [
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
  [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
  [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
  [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
  [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
  [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
  [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
  [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
  [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
  [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
  // The last two rounds repeat the first two, which is what the twelve-round
  // schedule means for a ten-entry table.
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
  [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
];

/** How many bytes the compression function consumes at a time. */
const BLOCK_BYTES = 128;

/** The largest digest BLAKE2b produces. */
const MAX_DIGEST_BYTES = 64;

/** Sixty-four bits of ones, for masking after every addition and rotation. */
const MASK64 = (1n << 64n) - 1n;

/** The rotation amounts the mixing function uses, in order. */
const ROTATIONS = [32n, 24n, 16n, 63n] as const;

/** Rotate a 64-bit word right. */
function rotr(value: bigint, bits: bigint): bigint {
  return ((value >> bits) | (value << (64n - bits))) & MASK64;
}

/** The mixing function, applied to four words of the working vector. */
function mix(v: bigint[], a: number, b: number, c: number, d: number, x: bigint, y: bigint): void {
  const [r1, r2, r3, r4] = ROTATIONS;
  v[a] = ((v[a] as bigint) + (v[b] as bigint) + x) & MASK64;
  v[d] = rotr((v[d] as bigint) ^ (v[a] as bigint), r1);
  v[c] = ((v[c] as bigint) + (v[d] as bigint)) & MASK64;
  v[b] = rotr((v[b] as bigint) ^ (v[c] as bigint), r2);
  v[a] = ((v[a] as bigint) + (v[b] as bigint) + y) & MASK64;
  v[d] = rotr((v[d] as bigint) ^ (v[a] as bigint), r3);
  v[c] = ((v[c] as bigint) + (v[d] as bigint)) & MASK64;
  v[b] = rotr((v[b] as bigint) ^ (v[c] as bigint), r4);
}

/**
 * Compress one block into the state.
 *
 * `counter` is the total number of bytes taken *including* this block, and
 * `last` marks the final block — both are mixed in, which is what stops a
 * message being extended.
 */
function compress(h: bigint[], block: Uint8Array, counter: bigint, last: boolean): void {
  const m: bigint[] = [];
  const view = new DataView(block.buffer, block.byteOffset, block.byteLength);
  for (let index = 0; index < 16; index += 1) {
    // Little-endian, which is the one place a big-endian reading would still
    // produce a plausible-looking digest.
    m.push(view.getBigUint64(index * 8, true));
  }

  const v: bigint[] = [...h, ...IV];
  v[12] = (v[12] as bigint) ^ (counter & MASK64);
  v[13] = (v[13] as bigint) ^ ((counter >> 64n) & MASK64);
  if (last) {
    v[14] = (v[14] as bigint) ^ MASK64;
  }

  for (const schedule of SIGMA) {
    mix(v, 0, 4, 8, 12, m[schedule[0] as number] as bigint, m[schedule[1] as number] as bigint);
    mix(v, 1, 5, 9, 13, m[schedule[2] as number] as bigint, m[schedule[3] as number] as bigint);
    mix(v, 2, 6, 10, 14, m[schedule[4] as number] as bigint, m[schedule[5] as number] as bigint);
    mix(v, 3, 7, 11, 15, m[schedule[6] as number] as bigint, m[schedule[7] as number] as bigint);
    mix(v, 0, 5, 10, 15, m[schedule[8] as number] as bigint, m[schedule[9] as number] as bigint);
    mix(v, 1, 6, 11, 12, m[schedule[10] as number] as bigint, m[schedule[11] as number] as bigint);
    mix(v, 2, 7, 8, 13, m[schedule[12] as number] as bigint, m[schedule[13] as number] as bigint);
    mix(v, 3, 4, 9, 14, m[schedule[14] as number] as bigint, m[schedule[15] as number] as bigint);
  }

  for (let index = 0; index < 8; index += 1) {
    h[index] = (h[index] as bigint) ^ (v[index] as bigint) ^ (v[index + 8] as bigint);
  }
}

/**
 * The BLAKE2b digest of `message`.
 *
 * @param digestBytes How many bytes to produce, from 1 to 64. Part of the
 *   hash rather than a truncation of it: two lengths give unrelated digests.
 * @throws {RangeError} For a length the algorithm does not have — clamping
 *   silently would produce a digest no other implementation agrees with.
 */
export function blake2b(message: Uint8Array, digestBytes = MAX_DIGEST_BYTES): Uint8Array {
  if (!Number.isInteger(digestBytes) || digestBytes < 1 || digestBytes > MAX_DIGEST_BYTES) {
    throw new RangeError(`BLAKE2b digest length must be 1 to ${MAX_DIGEST_BYTES} bytes (got ${digestBytes})`);
  }

  const h = [...IV];
  // The parameter block: digest length, key length (none), fanout and depth
  // both one for a plain hash. This is where the output length enters the
  // hash, and why a shorter digest is not a prefix of a longer one.
  h[0] = (h[0] as bigint) ^ 0x01010000n ^ BigInt(digestBytes);

  // Every block but the last is compressed whole; the last is padded with
  // zeros and marked, so a message ending on a block boundary and one a byte
  // shorter do not collide.
  let offset = 0;
  while (message.length - offset > BLOCK_BYTES) {
    compress(h, message.subarray(offset, offset + BLOCK_BYTES), BigInt(offset + BLOCK_BYTES), false);
    offset += BLOCK_BYTES;
  }

  const final = new Uint8Array(BLOCK_BYTES);
  final.set(message.subarray(offset));
  compress(h, final, BigInt(message.length), true);

  const digest = new Uint8Array(MAX_DIGEST_BYTES);
  const out = new DataView(digest.buffer);
  for (let index = 0; index < 8; index += 1) {
    out.setBigUint64(index * 8, h[index] as bigint, true);
  }
  return digest.subarray(0, digestBytes);
}
