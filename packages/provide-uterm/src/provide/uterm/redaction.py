#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Reusable redaction helpers for terminal logs and captures."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


# Containers nested this deep are not walked. Wire frames are JSON-decoded and
# shallow in practice (two or three levels); the cap exists so a hostile frame
# nested thousands deep cannot drive a RecursionError through every recorder
# and broadcast that redacts it. It is shared by the session recorder and the
# hub so the two cannot drift apart again.
REDACT_MAX_DEPTH = 32

# What a container at or past REDACT_MAX_DEPTH becomes. The subtree is replaced
# whole rather than passed through: an unwalked subtree is an unredacted one,
# and returning it verbatim would make "nest your secret 33 levels deep" a
# redaction bypass. A fixed marker (rather than serialising the subtree and
# redacting the string) also keeps the Go/TypeScript/C# ports byte-identical,
# since their JSON serialisers disagree on spacing and key order.
DEPTH_LIMIT_MARKER = "[NESTED_REDACTED]"

# What the value of a sensitive key becomes under key-aware redaction.
SENSITIVE_KEY_MARKER = "[SECRET_REDACTED]"

# Field names whose value is a credential regardless of what it looks like.
# The default text rules need context such as ``token=`` or ``password:`` to
# fire, so a bare ``{"token": "abc"}`` (a ``resume`` / ``session_token`` frame)
# passes them untouched. A superset of the connector-config keys masked by the
# server's tunnel routes (password/passphrase/secret/token). Matching is on the
# lower-cased key with ``-`` folded to ``_``.
SENSITIVE_KEY_NAMES = frozenset(
    {
        "token",
        "password",
        "passwd",
        "passphrase",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "private_key",
    }
)
# ...and any key ending in one of these (resume_token, share_token,
# client_secret, ssh_passphrase, ...).
SENSITIVE_KEY_SUFFIXES = ("_token", "_password", "_passphrase", "_secret", "_api_key")


def make_redactor(patterns: Sequence[str] | None = None) -> Callable[[str], str]:
    """Build a text redactor from regex patterns."""
    compiled = [re.compile(pattern) for pattern in patterns or ()]
    if not compiled:
        return lambda text: text

    def _redact(text: str) -> str:
        result = text
        for pattern in compiled:
            result = pattern.sub("[REDACTED]", result)
        return result

    return _redact


def redact_text(text: str, redactor: Callable[[str], str] | None) -> str:
    """Apply *redactor* to *text*, preserving identity when no redactor is configured."""
    if redactor is None:
        return text
    return redactor(text)


def is_sensitive_key(key: str) -> bool:
    """Return True when *key* names a credential field (see ``SENSITIVE_KEY_NAMES``)."""
    folded = key.lower().replace("-", "_")
    return folded in SENSITIVE_KEY_NAMES or folded.endswith(SENSITIVE_KEY_SUFFIXES)


def redact_structure(
    value: Any,
    redactor: Callable[[str], str],
    *,
    key_aware: bool = False,
    max_depth: int = REDACT_MAX_DEPTH,
    _depth: int = 0,
) -> Any:
    """Return a redacted copy of a JSON-like structure.

    - Strings go through *redactor* at any depth.
    - Dicts, lists and tuples are rebuilt (tuples become lists, as JSON would
      serialise them); dict keys are kept. The input is never mutated.
    - A container *max_depth* levels below the root (the root is level 0) is
      replaced whole by :data:`DEPTH_LIMIT_MARKER` — never returned unwalked.
    - With *key_aware*, a dict value under a sensitive key
      (:func:`is_sensitive_key`) becomes :data:`SENSITIVE_KEY_MARKER` unless
      it is ``None``, which stays ``None`` so "no token" is still visible.
    - Every other scalar (int/float/bool/None) is returned unchanged.

    Recursion is bounded by *max_depth*, so no input can raise RecursionError.
    """
    if isinstance(value, str):
        return redactor(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    if _depth >= max_depth:
        return DEPTH_LIMIT_MARKER
    child = _depth + 1
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if key_aware and item is not None and isinstance(key, str) and is_sensitive_key(key):
                out[key] = SENSITIVE_KEY_MARKER
            else:
                out[key] = redact_structure(item, redactor, key_aware=key_aware, max_depth=max_depth, _depth=child)
        return out
    return [redact_structure(item, redactor, key_aware=key_aware, max_depth=max_depth, _depth=child) for item in value]
