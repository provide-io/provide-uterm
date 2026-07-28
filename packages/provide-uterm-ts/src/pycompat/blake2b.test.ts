//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { blake2b } from "./index.ts";

interface HashGolden {
  digest_hex_length: number;
  digests: Array<{ input: string; hex: string }>;
}

const golden = loadGolden<HashGolden>("tokenhash_golden.json");

/** The hex digest of a string, as the token store takes it. */
function hex(value: string, digestBytes = 32): string {
  return Buffer.from(blake2b(Buffer.from(value, "utf8"), digestBytes)).toString("hex");
}

describe("BLAKE2b", () => {
  it.each(golden.digests)("hashes $input", (record) => {
    expect(hex(record.input)).toBe(record.hex);
  });

  it("is not the host's 64-byte digest truncated", () => {
    // The whole reason this exists. BLAKE2b mixes its output length into the
    // parameter block, so a 32-byte digest is not the first half of a 64-byte
    // one — a port that truncated would disagree from the first byte, and
    // only for shared sessions.
    const truncated = createHash("blake2b512").update("hello").digest("hex").slice(0, 64);
    expect(hex("hello")).not.toBe(truncated);
    expect(hex("hello")).toBe(golden.digests.find((entry) => entry.input === "hello")?.hex);
  });

  it("agrees with the host on the length it does offer", () => {
    // A check on the implementation itself rather than on the reference: at
    // 64 bytes there *is* something to compare against, and getting that
    // right is most of getting the rest right.
    for (const value of ["", "a", "hello world", "x".repeat(200)]) {
      expect(Buffer.from(blake2b(Buffer.from(value, "utf8"), 64)).toString("hex")).toBe(
        createHash("blake2b512").update(value).digest("hex"),
      );
    }
  });

  it("produces the digest length it was asked for", () => {
    for (const size of [1, 16, 32, 48, 64]) {
      expect(blake2b(Buffer.from("hello"), size)).toHaveLength(size);
    }
    expect(golden.digest_hex_length).toBe(64);
  });

  it("gives a different digest for each length", () => {
    // Not a prefix relationship: the length is part of the hash.
    const short = Buffer.from(blake2b(Buffer.from("hello"), 32)).toString("hex");
    const long = Buffer.from(blake2b(Buffer.from("hello"), 64)).toString("hex");
    expect(long.startsWith(short)).toBe(false);
  });

  it("refuses a digest length the algorithm does not have", () => {
    // Zero and anything past 64 are not BLAKE2b outputs; silently clamping
    // would produce a digest that no other implementation agrees with.
    for (const size of [0, -1, 65, 128]) {
      expect(() => blake2b(Buffer.from("hello"), size)).toThrow();
    }
  });

  it("walks the block boundary", () => {
    // Where an implementation that mis-counts is wrong for some lengths and
    // right for others.
    for (const length of [127, 128, 129, 255, 256, 257]) {
      const input = "x".repeat(length);
      expect(hex(input)).toBe(golden.digests.find((entry) => entry.input === input)?.hex);
    }
  });

  it("hashes the empty input", () => {
    // The one case with no blocks of data at all, which a loop written
    // around "while there is more" gets wrong.
    expect(hex("")).toBe(golden.digests.find((entry) => entry.input === "")?.hex);
  });

  it("hashes bytes, not characters", () => {
    // Non-ASCII is encoded as UTF-8 first; hashing code units would give a
    // digest no other implementation produces.
    expect(hex("héllo → ✓")).toBe(golden.digests.find((entry) => entry.input === "héllo → ✓")?.hex);
  });
});
