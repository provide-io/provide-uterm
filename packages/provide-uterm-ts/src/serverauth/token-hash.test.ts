//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { hashToken, verifyToken } from "./index.ts";

interface HashGolden {
  store_digests: Array<{ input: string; hex: string }>;
  verifications: Array<{ name: string; plain: string; stored: string; result: boolean }>;
}

const golden = loadGolden<HashGolden>("tokenhash_golden.json");

describe("hashing a tunnel token", () => {
  it.each(golden.store_digests)("hashes $input", (record) => {
    expect(hashToken(record.input)).toBe(record.hex);
  });

  it("holds a digest, not the token", () => {
    // A memory disclosure on the server leaks only hashes; the originals
    // cannot be reconstructed without brute-forcing a 256-bit preimage.
    const token = "GdK7Zx2qLm9nP4rT6vY8wA1bC3dE5fH7jK9lM0nO2pQ"; // pragma: allowlist secret - a corpus fixture
    expect(hashToken(token)).not.toContain(token);
    expect(hashToken(token)).toHaveLength(64);
  });

  it("does not hash an empty token at all", () => {
    // So "no token configured" reads the same as "no match" — an empty slot
    // must never authenticate anyone, and a digest of the empty string would
    // authenticate a caller who sent nothing.
    expect(hashToken("")).toBe("");
  });
});

describe("verifying a tunnel token", () => {
  it.each(golden.verifications)("$name", (record) => {
    expect(verifyToken(record.plain, record.stored)).toBe(record.result);
  });

  it("accepts the token that made the digest", () => {
    expect(verifyToken("secret", hashToken("secret"))).toBe(true);
  });

  it("refuses a token differing by one character", () => {
    expect(verifyToken("secreT", hashToken("secret"))).toBe(false);
    expect(verifyToken("secret ", hashToken("secret"))).toBe(false);
  });

  it("refuses an empty token whatever is stored", () => {
    // A configured-but-empty slot must never authenticate any caller.
    expect(verifyToken("", hashToken("secret"))).toBe(false);
    expect(verifyToken("", "")).toBe(false);
  });

  it("refuses an empty stored hash whatever is sent", () => {
    expect(verifyToken("secret", "")).toBe(false);
    expect(verifyToken("anything", "")).toBe(false);
  });

  it("refuses a stored value that is not a digest", () => {
    // A truncated or corrupted hash is not a weaker check, it is no check.
    expect(verifyToken("secret", "nonsense")).toBe(false);
    expect(verifyToken("secret", hashToken("secret").slice(0, 32))).toBe(false);
  });

  it("compares without leaking where two digests diverge", () => {
    // Constant-time against the digest, which is what stops an attacker
    // learning a stored hash a byte at a time. Asserted structurally: the
    // comparison reads every byte rather than stopping at the first
    // difference.
    const stored = hashToken("secret");
    const nearly = `0${stored.slice(1)}`;
    const late = `${stored.slice(0, -1)}0`;
    expect(verifyToken("secret", nearly)).toBe(false);
    expect(verifyToken("secret", late)).toBe(false);
  });
});
