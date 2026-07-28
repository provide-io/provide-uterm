#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for tunnel token hashing.

Share and control tokens are held as digests, not as tokens: a memory
disclosure on the server leaks only hashes, and the originals cannot be
reconstructed without brute-forcing a 256-bit preimage.

**The digest length is part of the hash, not a truncation of it.** BLAKE2b
mixes its output length into the parameter block, so a 32-byte digest is not
the first 32 bytes of a 64-byte one. That is why this corpus exists at all:
the host runtime offers only the 64-byte variant, and a port that truncated it
would disagree from the first byte — silently, and only for shared sessions.

**An empty token and an empty stored hash both fail.** A configured-but-empty
slot must never authenticate any caller, and "no token configured" has to read
the same as "no match".

The block boundary is where a hash implementation goes wrong, so the inputs
walk across it: 127, 128 and 129 bytes, and again either side of two blocks.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_tokenhash_golden.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from provide.uterm.tunnel.token_hash import hash_token, verify_token

OUT = Path(__file__).with_name("tokenhash_golden.json")

# Inputs chosen to walk the compression function's block boundary, where an
# implementation that mis-counts is wrong for some lengths and right for
# others.
INPUTS: list[str] = [
    "",
    "a",
    "hello",
    "hello world",
    # A token of the shape the server actually issues.
    "GdK7Zx2qLm9nP4rT6vY8wA1bC3dE5fH7jK9lM0nO2pQ",
    # Non-ASCII, which is encoded as UTF-8 before hashing.
    "héllo → ✓",
    "🔑" * 8,
    # Either side of one block.
    "x" * 127,
    "x" * 128,
    "x" * 129,
    # Either side of two.
    "x" * 255,
    "x" * 256,
    "x" * 257,
    # Long enough to need many blocks.
    "x" * 1000,
    # A single byte at every bit position, to catch a mis-set parameter block.
    "\x00",
    "\x01",
    "\x80",
    "\xff",
]

# (name, plain, stored) — what authenticates and what does not.
VERIFY_CASES: list[tuple[str, str, str]] = [
    ("the right token", "secret", hash_token("secret")),
    ("the wrong token", "wrong", hash_token("secret")),
    ("an empty token", "", hash_token("secret")),
    ("an empty stored hash", "secret", ""),
    ("both empty", "", ""),
    ("a stored hash that is not a hash", "secret", "nonsense"),
    ("a stored hash of the wrong length", "secret", hash_token("secret")[:32]),
    ("a token differing in one character", "secreT", hash_token("secret")),
    ("a token with trailing space", "secret ", hash_token("secret")),
]


def _build() -> dict[str, Any]:
    """Every digest and every verification."""
    return {
        "digest_hex_length": len(hash_token("x")),
        # The hash itself, which has a digest for the empty input like any
        # other. Kept apart from the token store's own answer below, because
        # the store deliberately short-circuits an empty token.
        "digests": [
            {"input": value, "hex": hashlib.blake2b(value.encode("utf-8"), digest_size=32).hexdigest()}
            for value in INPUTS
        ],
        # The store's rule: an empty token is not hashed at all, so "no token
        # configured" reads the same as "no match".
        "store_digests": [{"input": value, "hex": hash_token(value)} for value in INPUTS[:4]],
        "verifications": [
            {"name": name, "plain": plain, "stored": stored, "result": verify_token(plain, stored)}
            for name, plain, stored in VERIFY_CASES
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(INPUTS)} digests, {len(VERIFY_CASES)} verifications)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
