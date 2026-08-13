#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the tunnel token-hash golden from the REAL Python reference
(provide.uterm.tunnel.token_hash.hash_token). Run from the repo root:

    uv run python packages/provide-uterm-go/tunnel/testdata/gen_token_hash_golden.py

Writes token_hash_golden.json next to this script. Go's tunnel.HashToken must
reproduce every digest here, so a drift in either implementation fails CI.

The PLAINTEXTS are a fixed list — they are the test inputs, and pinning them is
what makes the corpus stable. The DIGESTS are recomputed from the reference on
every run, which is the whole point: if hash_token ever changes algorithm,
digest size, or encoding, this file moves and .ci/check_goldens.sh fails.
"""

from __future__ import annotations

import json
import pathlib

from provide.uterm.tunnel.token_hash import hash_token

# Spans: an ordinary token, the empty string (hash_token's documented
# "no token configured" case, which returns "" rather than a digest), a
# single byte, non-ASCII (UTF-8 encoding is part of the contract), the full
# base64url alphabet, surrounding whitespace (NOT stripped — a token is
# compared verbatim), and five realistic 43-char base64url secrets of the
# shape the tunnel actually mints.
PLAINTEXTS = [
    "hello-token-123",
    "",
    "a",
    "unicode-éè-token",
    "AaBbCc_-0123456789",
    " spaces around ",
    "71z2O6UAyE3SCRO6lqVuFQacw1AxT3owvJaqsyMXNDQ",
    "xZYNaC6eQT8qWzUFVOgLAPjvhidg2rwyetPk-Ta_Pwo",
    "qqpxFj5rDRblRN5t1dELChbRPiqHx7ryxVG7_Trv1i0",
    "FZXhD42JrxKwGbcy5c87FqkBuuK6AtfBQuMu8SBQ3xc",
    "SzbOHoQiSnrWjdAu9XNRQGGLYOBvRwz5H1b3AmQWCl0",
]

NOTE = "blake2b-256 hex of plain (Python tunnel/token_hash.hash_token). Go tunnel.HashToken must match byte-for-byte."


def main() -> None:
    corpus = {
        "note": NOTE,
        "cases": [{"plain": plain, "blake2b_hex": hash_token(plain)} for plain in PLAINTEXTS],
    }
    out = pathlib.Path(__file__).with_name("token_hash_golden.json")
    out.write_text(json.dumps(corpus, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(corpus['cases'])} cases)")


if __name__ == "__main__":
    main()
