#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Reusable redaction helpers for terminal logs and captures."""

from __future__ import annotations

import re
from itertools import pairwise
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

# Key-aware redaction: which field names hold a credential regardless of what
# the value looks like. The default text rules need context such as ``token=``
# or ``password:`` to fire, so a bare ``{"token": "abc"}`` (a ``resume`` /
# ``session_token`` frame) passes them untouched.
#
# The rule works on a key's *segments*: camelCase is split (``accessToken`` ->
# ``access``, ``token``; ``APIKey`` -> ``api``, ``key``), the result is
# lower-cased, ``-``, ``.`` and spaces become ``_``, and it is split on ``_``.
# A key is sensitive when
#   - any segment is in SENSITIVE_KEY_SEGMENTS (``resume_token``,
#     ``Set-Cookie``, ``Proxy-Authorization``, ``aws_secret_access_key``), or
#   - two adjacent segments form a pair in SENSITIVE_KEY_PAIRS (``api_key``,
#     ``privateKey``, ``x-api-key``, ``access_key_id``'s ``access_key``),
# unless its LAST segment is in NON_SECRET_KEY_SUFFIXES: a field that describes
# a credential rather than holding one (``token_count``, ``token_type``,
# ``password_required``, ``secret_hash``) stays visible.
# Whole segments only: ``tokens``, ``max_tokens``, ``tokenizer`` and
# ``secretary`` are not sensitive. ``session_id`` is deliberately not covered:
# it names a session on nearly every frame and is not a bearer credential.
# A superset of the connector-config keys the server's tunnel routes mask
# (password/passphrase/secret/token). The Go, TypeScript and C# ports implement
# the same rule; the shared session-logger corpus pins it.
SENSITIVE_KEY_SEGMENTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "cookie",
        "authorization",
        "apikey",
    }
)
SENSITIVE_KEY_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key")})
NON_SECRET_KEY_SUFFIXES = frozenset(
    {"count", "len", "length", "size", "type", "kind", "ttl", "expiry", "expires", "hash", "required", "enabled"}
)


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


def _is_upper(ch: str) -> bool:
    return "A" <= ch <= "Z"


def _is_lower_or_digit(ch: str) -> bool:
    return "a" <= ch <= "z" or "0" <= ch <= "9"


def key_segments(key: str) -> list[str]:
    """Split *key* into lower-case segments (see the rule above ``SENSITIVE_KEY_SEGMENTS``).

    An ASCII upper-case letter starts a new segment when it follows a lower-case
    letter or digit (``accessToken``) or ends an acronym (the ``K`` in
    ``APIKey``). ``-``, ``.`` and spaces are separators like ``_``.
    """
    chars: list[str] = []
    last = len(key) - 1
    for i, ch in enumerate(key):
        if i > 0 and _is_upper(ch):
            prev = key[i - 1]
            ends_acronym = _is_upper(prev) and i < last and "a" <= key[i + 1] <= "z"
            if _is_lower_or_digit(prev) or ends_acronym:
                chars.append("_")
        chars.append(ch)
    folded = "".join(chars).lower().replace("-", "_").replace(".", "_").replace(" ", "_")
    return [segment for segment in folded.split("_") if segment]


def is_sensitive_key(key: str) -> bool:
    """Return True when *key* names a credential field (see ``SENSITIVE_KEY_SEGMENTS``)."""
    segments = key_segments(key)
    if not segments or segments[-1] in NON_SECRET_KEY_SUFFIXES:
        return False
    if any(segment in SENSITIVE_KEY_SEGMENTS for segment in segments):
        return True
    return any(pair in SENSITIVE_KEY_PAIRS for pair in pairwise(segments))


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
