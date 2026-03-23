# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Bounded queue controls for telemetry signal paths."""

from __future__ import annotations

__all__ = [
    "QueuePolicy",
    "QueueTicket",
    "get_queue_policy",
    "release",
    "set_queue_policy",
    "try_acquire",
]

import itertools
import threading
from collections import deque
from dataclasses import dataclass

from provide.telemetry.health import increment_dropped, set_queue_depth

Signal = str


@dataclass(frozen=True)
class QueuePolicy:
    logs_maxsize: int = 0
    traces_maxsize: int = 0
    metrics_maxsize: int = 0


@dataclass(frozen=True)
class QueueTicket:
    signal: Signal
    token: int


_lock = threading.Lock()
_tokens = itertools.count(1)
_policy = QueuePolicy()
_queues: dict[Signal, deque[int]] = {
    "logs": deque(),
    "traces": deque(),
    "metrics": deque(),
}
_VALID_SIGNALS = frozenset(_queues)


def _validate_signal(signal: Signal) -> Signal:
    if signal not in _VALID_SIGNALS:
        raise ValueError(f"unknown signal {signal!r}, expected one of {sorted(_VALID_SIGNALS)}")
    return signal


def set_queue_policy(policy: QueuePolicy) -> None:
    global _policy
    with _lock:
        _policy = policy
        for signal in ("logs", "traces", "metrics"):
            _queues[signal].clear()
            set_queue_depth(signal, 0)


def get_queue_policy() -> QueuePolicy:
    with _lock:
        return _policy


def _maxsize(signal: Signal) -> int:
    if signal == "traces":
        return _policy.traces_maxsize
    if signal == "metrics":
        return _policy.metrics_maxsize
    return _policy.logs_maxsize


def try_acquire(signal: Signal) -> QueueTicket | None:
    signal = _validate_signal(signal)
    with _lock:
        maxsize = _maxsize(signal)
        if maxsize <= 0:
            return QueueTicket(signal=signal, token=0)  # pragma: no mutate
        queue = _queues[signal]
        if len(queue) >= maxsize:
            increment_dropped(signal)
            return None
        token = next(_tokens)
        queue.append(token)
        set_queue_depth(signal, len(queue))
        return QueueTicket(signal=signal, token=token)


def release(ticket: QueueTicket | None) -> None:
    if ticket is None:
        return
    if ticket.token == 0:  # pragma: no mutate
        return
    with _lock:
        queue = _queues[_validate_signal(ticket.signal)]
        try:
            queue.remove(ticket.token)
        except ValueError:
            return
        set_queue_depth(ticket.signal, len(queue))


def reset_queues_for_tests() -> None:
    set_queue_policy(QueuePolicy())
