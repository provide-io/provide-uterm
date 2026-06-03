#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Structural ReDoS guard for untrusted (LLM/user-supplied) regex patterns.

The MCP tool surface lets a caller — including a low-privilege ``viewer`` role
via ``session_watch`` / ``session_subscribe`` — hand us a regex that we compile
with stdlib :mod:`re`.  A pure length cap does not bound catastrophic
backtracking for *short* pathological patterns such as ``(a+)+$``, so before any
``re.compile`` we reject the cheap, well-known catastrophic constructs:

* **nested quantifiers** — a quantified group whose body is itself quantified
  (``(a+)+``, ``(a*)*``, ``(a+)*``, ``(a*)+``, ``(\\w+)+`` …), the classic
  exponential-backtracking shape; and
* **quantified backreferences** — ``\\1+``, ``(\\1)+`` …, which also drive
  pathological backtracking.

Neither stdlib :mod:`re` (no time bound) nor a new third-party engine
(``re2``/``regex`` are not project dependencies) is used: this is a structural
denylist applied *in addition to* the length cap.

Residual risk (documented honestly): this does NOT catch every ReDoS shape —
notably overlapping alternations like ``(a|a)*`` or quantifier interactions
across adjacent tokens — because detecting those structurally without large
false-positive collateral is hard.  It removes the cheap, classic amplification
paths an LLM is most likely to emit; egress/time bounds at the server remain the
defence-in-depth backstop.
"""

from __future__ import annotations

import re

# A backreference token: ``\1`` .. ``\99`` (stdlib ``re`` allows up to 99 groups).
_BACKREF = re.compile(r"\\[1-9][0-9]?$")

# A quantified backreference *anywhere* in the pattern: ``\1+``, ``\2*``,
# ``\3{2,5}`` — the ``\`` is not itself escaped because the scan below skips
# escaped pairs when collecting groups, but a top-level backref+quantifier needs
# its own check (it is not wrapped in a group).
_QUANTIFIED_BACKREF = re.compile(r"\\[1-9][0-9]?[+*{]")

# Quantifier characters that, when they immediately follow a group close paren,
# make that group "repeated".
_QUANTIFIER_OPENERS = frozenset("+*{")


def _group_bodies_with_following_char(pattern: str) -> list[tuple[str, str]]:
    """Return ``(body, char_after_close)`` for every balanced ``(...)`` group.

    Escaped parens (``\\(`` / ``\\)``) are treated as literals, not group
    delimiters.  Unbalanced closes are ignored (the downstream stdlib compiler
    rejects the pattern as invalid), so this never raises on malformed input.
    """
    stack: list[int] = []
    results: list[tuple[str, str]] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "\\":
            # Skip the escaped character so ``\(`` / ``\)`` are literals.
            i += 2
            continue
        if char == "(":
            stack.append(i)
        elif char == ")" and stack:
            start = stack.pop()
            body = pattern[start + 1 : i]
            after = pattern[i + 1] if i + 1 < n else ""
            results.append((body, after))
        i += 1
    return results


def _body_is_repeated_unit(body: str) -> bool:
    """Return ``True`` when a group *body* is itself a repeated/backref unit.

    Such a body, when the enclosing group is also quantified, forms the classic
    nested-quantifier (``(a+)+``) or quantified-backref-in-group (``(\\1)+``)
    catastrophic shape.
    """
    if not body:
        return False
    # The whole body is a backreference, e.g. ``(\1)+``.
    if _BACKREF.fullmatch(body):
        return True
    last = body[-1]
    # A lazy quantifier (``a+?``) ends in ``?``; the real quantifier is the
    # char before it.
    if last == "?":
        return len(body) >= 2 and body[-2] in "+*}"
    if last not in "+*}":
        return False
    # Guard against an escaped quantifier (``a\*``) being read as a quantifier.
    return not (len(body) >= 2 and body[-2] == "\\")


def has_catastrophic_construct(pattern: str) -> bool:
    """Return ``True`` when *pattern* contains a known catastrophic construct.

    Detects nested quantifiers (a quantified group whose body is itself
    quantified) and quantified backreferences.  See the module docstring for the
    residual-risk caveats — this is a structural denylist, not a proof of
    linear-time matching.
    """
    if _QUANTIFIED_BACKREF.search(pattern):
        return True
    for body, after in _group_bodies_with_following_char(pattern):
        if after in _QUANTIFIER_OPENERS and _body_is_repeated_unit(body):
            return True
    return False
