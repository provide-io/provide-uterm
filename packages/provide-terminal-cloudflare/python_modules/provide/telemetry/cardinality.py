# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Attribute cardinality guardrails."""

from __future__ import annotations

__all__ = [
    "OVERFLOW_VALUE",
    "CardinalityLimit",
    "clear_cardinality_limits",
    "get_cardinality_limits",
    "guard_attributes",
    "register_cardinality_limit",
]

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CardinalityLimit:
    max_values: int
    ttl_seconds: float = 300.0


_lock = threading.Lock()
_limits: dict[str, CardinalityLimit] = {}
_seen: dict[str, dict[str, float]] = {}
_last_prune: dict[str, float] = {}
_PRUNE_INTERVAL = 5.0  # seconds between prune sweeps per key
OVERFLOW_VALUE = "__overflow__"


def register_cardinality_limit(key: str, max_values: int, ttl_seconds: float = 300.0) -> None:  # pragma: no mutate
    with _lock:
        _limits[key] = CardinalityLimit(max_values=max(1, max_values), ttl_seconds=max(1.0, ttl_seconds))
        _seen.setdefault(key, {})


def get_cardinality_limits() -> dict[str, CardinalityLimit]:
    with _lock:
        return dict(_limits)


def clear_cardinality_limits() -> None:
    with _lock:
        _limits.clear()
        _seen.clear()
        _last_prune.clear()


def _prune_expired(key: str, now: float) -> None:
    limit = _limits.get(key)
    seen = _seen.get(key)
    if limit is None or seen is None:
        return
    threshold = now - limit.ttl_seconds
    for value, seen_at in list(seen.items()):
        if seen_at < threshold:
            del seen[value]


def guard_attributes(attributes: dict[str, str]) -> dict[str, str]:
    now = time.monotonic()
    with _lock:
        if not _limits:
            return attributes
        guarded = dict(attributes)
        for key, value in list(guarded.items()):
            limit = _limits.get(key)
            if limit is None:
                continue
            if now - _last_prune.get(key, 0.0) >= _PRUNE_INTERVAL:  # pragma: no mutate
                _prune_expired(key, now)
                _last_prune[key] = now  # pragma: no mutate
            seen = _seen.setdefault(key, {})
            if value in seen:
                seen[value] = now  # pragma: no mutate
                continue
            if len(seen) >= limit.max_values:
                guarded[key] = OVERFLOW_VALUE
                continue
            seen[value] = now  # pragma: no mutate
        return guarded
