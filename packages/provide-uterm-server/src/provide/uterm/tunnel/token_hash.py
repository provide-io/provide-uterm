#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hash-based at-rest storage for tunnel bearer tokens.

The hub holds tunnel tokens in process memory. A memory disclosure (core
dump, debugger attach, log of object state) on the server would otherwise
leak every active share/control token verbatim. Hashing the tokens before
storage means a disclosure leaks only digests; the original tokens cannot
be reconstructed without brute-forcing the BLAKE2b preimage, which is
infeasible for the 256-bit ``secrets.token_urlsafe(32)`` values that
``routes/tunnels.py`` issues.

Two-call API: ``hash_token`` is used at issuance time before storing the
hash; ``verify_token`` is used at every authentication site to compare a
caller-supplied plain token against the stored hash, in constant time.
"""

from __future__ import annotations

import hashlib
import secrets

# 32-byte digest is the BLAKE2b output that matches the entropy of the
# 32-byte urlsafe token we issue. Larger is wasteful, smaller is weaker.
_DIGEST_BYTES = 32


def hash_token(plain: str) -> str:
    """Return the BLAKE2b hex digest of ``plain``.

    Returns the empty string for an empty/None token so callers can treat
    "no token configured" the same as "no match".
    """
    if not plain:
        return ""
    return hashlib.blake2b(plain.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def verify_token(plain: str, stored_hash: str) -> bool:
    """Constant-time compare ``plain``'s hash against ``stored_hash``.

    Both the empty-stored-hash case and the empty-plain case return False —
    a configured-but-empty slot must never authenticate any caller.
    """
    if not plain or not stored_hash:
        return False
    candidate = hash_token(plain)
    return secrets.compare_digest(candidate, stored_hash)
