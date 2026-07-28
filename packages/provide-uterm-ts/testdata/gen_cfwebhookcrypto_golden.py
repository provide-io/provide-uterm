#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for webhook secret encryption.

A webhook's secret is an HMAC signing key, so unlike a bearer token it has to
be recoverable in plaintext to sign each delivery — it cannot be one-way
hashed. Cloudflare encrypts Durable Object storage at rest already; this adds
AES-256-GCM on top, keyed by a Worker secret binding, so a raw dump of the
database never yields the signing keys.

**A deployment with no key keeps working.** The secret is stored and read back
as it is. That is the single-tenant case, and refusing to sign at all would be
worse than storing in the clear on storage that is already encrypted.

**A secret written before this existed is read unchanged.** There is no
migration step, so plaintext rows have to keep working.

**Anything that cannot be decrypted skips the signature rather than sending a
wrong one.** An envelope with no key configured, a malformed envelope, a
corrupted ciphertext, the wrong key — all return nothing. A delivery with a
signature that does not verify is worse than one with no signature, because a
receiver checking signatures would reject it while one that is not would trust
it.

The AES-GCM primitives themselves are marked no-cover in the reference: they
run only inside the Cloudflare Pyodide runtime and are exercised by its
integration tests. What is recorded here is the wiring — the key lookup, the
envelope, and the fallbacks — which is pure.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfwebhookcrypto_golden.py
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.do._webhook_crypto import (
    decrypt_secret,
    encrypt_secret,
    is_encrypted,
    webhook_key_b64,
)

OUT = Path(__file__).with_name("cfwebhookcrypto_golden.json")

# A base64 AES-256 key, as the binding would carry it. A corpus fixture.
KEY = base64.b64encode(bytes(range(32))).decode()


class _Env:
    """A Worker environment that may carry the key binding."""

    def __init__(self, key: Any = None) -> None:
        if key is not None:
            self.WEBHOOK_SECRET_KEY = key


# (name, binding) — what counts as a configured key.
KEY_CASES: list[tuple[str, Any]] = [
    ("a key", KEY),
    ("no binding at all", None),
    ("an empty binding", ""),
    ("a blank binding", "   "),
    ("a binding that is not a string", 12345),
    ("a binding that is false", False),
]

# (name, stored) — what carries the envelope.
ENVELOPE_CASES: list[tuple[str, str]] = [
    ("an envelope", "enc:v1:aXY=:Y3Q="),
    ("plaintext", "a-signing-secret"),
    ("nothing", ""),
    ("something that merely mentions it", "not-enc:v1:x"),
    ("a later version", "enc:v2:aXY=:Y3Q="),
    ("the prefix alone", "enc:v1:"),
]

# (name, stored) — reading a stored value back with no key configured.
READ_WITHOUT_KEY: list[tuple[str, str]] = [
    ("plaintext is returned as it is", "a-signing-secret"),
    ("an envelope yields nothing", "enc:v1:aXY=:Y3Q="),
    ("an empty value", ""),
]

# (name, stored) — reading with a key configured but the value unusable.
READ_WITH_KEY: list[tuple[str, str]] = [
    ("plaintext is still returned", "a-signing-secret"),
    ("an envelope with too few fields", "enc:v1:only-one"),
    ("an envelope with nothing after the prefix", "enc:v1:"),
    ("an envelope whose ciphertext is not base64", "enc:v1:!!!:???"),
    ("an envelope that decrypts to nothing", "enc:v1:aXY=:Y3Q="),
]


async def _build() -> dict[str, Any]:
    """Everything the wiring decides."""
    return {
        "key": KEY,
        "prefix": "enc:v1:",
        "keys": [
            {"name": name, "binding": binding, "result": webhook_key_b64(_Env(binding))} for name, binding in KEY_CASES
        ],
        "envelopes": [
            {"name": name, "stored": stored, "result": is_encrypted(stored)} for name, stored in ENVELOPE_CASES
        ],
        # With no key configured the secret is stored as it is.
        "encrypt_without_key": await encrypt_secret(_Env(), "a-signing-secret"),
        "encrypt_empty_without_key": await encrypt_secret(_Env(), ""),
        "read_without_key": [
            {"name": name, "stored": stored, "result": await decrypt_secret(_Env(), stored)}
            for name, stored in READ_WITHOUT_KEY
        ],
        "read_with_key": [
            {"name": name, "stored": stored, "result": await decrypt_secret(_Env(KEY), stored)}
            for name, stored in READ_WITH_KEY
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = asyncio.run(_build())
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(KEY_CASES)} key cases, {len(ENVELOPE_CASES)} envelope cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
