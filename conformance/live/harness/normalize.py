#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Masking and comparing what two cells of the matrix observed.

The comparison is the point of the harness. Sixteen cells — every client
language against every server language — must observe the same thing, and
anything that legitimately differs between two runs (a generated id, a clock)
has to be named by the scenario rather than guessed at here. A heuristic that
masked "anything that looks like an id" would eventually mask a divergence.

Two readings are carried over from :mod:`harness.expectations` deliberately,
because they are the same contract:

* a boolean is never a number, whatever Python thinks;
* a whole float and the integer are the same number, because JSON has one.

The difference between the two modules is that a missing field is a *failure*
against an expectation and a *difference* between two cells; both are visible,
neither is silently tolerated.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: What a value declared volatile is replaced with before comparison.
VOLATILE: Final = "<volatile>"


@dataclass(frozen=True)
class Difference:
    """One place two cells of the matrix did not agree."""

    path: str
    left: Any
    right: Any


def mask(fields: Mapping[str, Any], paths: Iterable[str]) -> dict[str, Any]:
    """Return a copy of *fields* with every declared path replaced.

    A path that is not there is left absent rather than created: masking an
    absent field into existence would hide the difference between a server
    that sent it and one that did not.

    A ``*`` segment stands for every element of a list or every value of a
    mapping, so a session list nobody can predict the length of is still
    comparable on everything except its ids.
    """
    masked = copy.deepcopy(dict(fields))
    for path in paths:
        _mask_one(masked, path.split("."))
    return masked


def _mask_one(node: Any, segments: Sequence[str]) -> None:
    """Replace the value *segments* names within *node*, in place."""
    head, rest = segments[0], segments[1:]
    if head == "*":
        for key in _keys_of(node):
            _apply(node, key, rest)
        return
    key = _key_in(node, head)
    if key is not None:
        _apply(node, key, rest)


def _apply(node: Any, key: Any, rest: Sequence[str]) -> None:
    """Descend to *rest* under *key*, or mask it when there is nothing left."""
    if rest:
        _mask_one(node[key], rest)
    else:
        node[key] = VOLATILE


def _keys_of(node: Any) -> list[Any]:
    """Every key a ``*`` stands for, or nothing when there is no way in."""
    if isinstance(node, Mapping):
        return list(node.keys())
    if isinstance(node, list):
        return list(range(len(node)))
    return []


def _key_in(node: Any, segment: str) -> Any | None:
    """The key *segment* names in *node*, or ``None`` when it names nothing."""
    if isinstance(node, Mapping):
        return segment if segment in node else None
    if isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
        return int(segment)
    return None


def observations(result: Mapping[str, Any], volatile: Mapping[str, Sequence[str]]) -> dict[str, dict[str, Any]]:
    """The steps of a driver's result, keyed by id and masked for comparison."""
    return {
        str(step["id"]): mask(step.get("fields", {}), volatile.get(str(step["id"]), ()))
        for step in result.get("steps", ())
    }


def differences(left: Any, right: Any, prefix: str = "") -> list[Difference]:
    """Every place *left* and *right* disagree, deepest path named."""
    if _same_shape(left, right):
        return _within(left, right, prefix)
    if _same_number(left, right):
        return []
    return [Difference(prefix, left, right)]


def _same_shape(left: Any, right: Any) -> bool:
    """Whether two values can be compared piece by piece rather than whole."""
    both_mappings = isinstance(left, Mapping) and isinstance(right, Mapping)
    both_lists = isinstance(left, list) and isinstance(right, list)
    return both_mappings or both_lists


def _within(left: Any, right: Any, prefix: str) -> list[Difference]:
    """The differences inside two values of the same shape."""
    if isinstance(left, Mapping):
        keys = list(left.keys()) + [key for key in right if key not in left]
        return [
            difference
            for key in keys
            for difference in differences(left.get(key, _ABSENT), right.get(key, _ABSENT), _join(prefix, str(key)))
        ]
    return [
        difference
        for index in range(max(len(left), len(right)))
        for difference in differences(
            left[index] if index < len(left) else _ABSENT,
            right[index] if index < len(right) else _ABSENT,
            _join(prefix, str(index)),
        )
    ]


def _same_number(left: Any, right: Any) -> bool:
    """Whether two scalars are the same JSON number — never a boolean."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def _join(prefix: str, segment: str) -> str:
    return f"{prefix}.{segment}" if prefix else segment


class _Absent:
    """A value one side never sent, which compares equal to nothing."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<absent>"


_ABSENT: Final = _Absent()
