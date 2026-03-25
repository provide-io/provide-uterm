# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Runtime sampling policy controls."""

from __future__ import annotations

__all__ = [
    "SamplingPolicy",
    "get_sampling_policy",
    "set_sampling_policy",
    "should_sample",
]

import logging
import random
import threading
from dataclasses import dataclass, field

from provide.telemetry.health import increment_dropped

_logger = logging.getLogger(__name__)

Signal = str


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    default_rate: float = 1.0
    overrides: dict[str, float] = field(default_factory=dict)


_lock = threading.Lock()
_policies: dict[Signal, SamplingPolicy] = {
    "logs": SamplingPolicy(),
    "traces": SamplingPolicy(),
    "metrics": SamplingPolicy(),
}
_VALID_SIGNALS = frozenset(_policies)


def _validate_signal(signal: Signal) -> Signal:
    if signal not in _VALID_SIGNALS:
        raise ValueError(f"unknown signal {signal!r}, expected one of {sorted(_VALID_SIGNALS)}")
    return signal


def _normalize_rate(rate: float) -> float:
    clamped = max(0.0, min(1.0, rate))
    if clamped != rate:
        _logger.warning("sampling.rate.clamped.warning")  # pragma: no mutate
    return clamped


def set_sampling_policy(signal: Signal, policy: SamplingPolicy) -> None:
    sig = _validate_signal(signal)
    normalized = SamplingPolicy(
        default_rate=_normalize_rate(policy.default_rate),
        overrides={k: _normalize_rate(v) for k, v in policy.overrides.items()},
    )
    with _lock:
        _policies[sig] = normalized


def get_sampling_policy(signal: Signal) -> SamplingPolicy:
    sig = _validate_signal(signal)
    with _lock:
        stored = _policies[sig]
        return SamplingPolicy(default_rate=stored.default_rate, overrides=dict(stored.overrides))


def should_sample(signal: Signal, key: str | None = None) -> bool:
    sig = _validate_signal(signal)
    with _lock:
        policy = _policies[sig]
    rate = policy.default_rate
    if key is not None and key in policy.overrides:
        rate = policy.overrides[key]
    rate = _normalize_rate(rate)
    if rate <= 0.0:  # pragma: no mutate
        keep = False
    elif rate >= 1.0:  # pragma: no mutate
        keep = True
    else:
        keep = random.random() < rate  # noqa: S311 - non-crypto telemetry sampling.
    if not keep:
        increment_dropped(signal)
    return keep


def reset_sampling_for_tests() -> None:
    with _lock:
        for signal in ("logs", "traces", "metrics"):
            _policies[signal] = SamplingPolicy()
