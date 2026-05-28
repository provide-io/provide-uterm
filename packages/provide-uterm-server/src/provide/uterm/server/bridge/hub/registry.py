#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WorkerRegistry: in-memory registry of attached workers.

Owns the lifecycle of :class:`WorkerTermState` instances keyed by
``worker_id``. This class is intentionally a thin wrapper around a
``dict`` — its purpose is to give the worker map a *name* that other
hub services can hold a reference to, instead of poking at
``TermHub._workers`` directly.

Lock semantics are unchanged from the pre-refactor design: lock-free
reads remain safe because CPython dict reads are atomic, and mutations
are coordinated by callers via :attr:`TermHub._lock`. The registry
itself takes no locks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from provide.uterm.server.bridge.models import WorkerTermState


class WorkerRegistry:
    """In-memory registry of attached workers, keyed by ``worker_id``.

    The underlying ``dict`` is exposed as :attr:`_workers` so existing
    hub mixin code can continue to use mapping operations
    (``setdefault``, ``items``, ``values``, indexed access, ``del``)
    directly during the phased refactor. New code should prefer the
    explicit methods (:meth:`get`, :meth:`put`, :meth:`pop`,
    :meth:`contains`, :meth:`all`, :meth:`items`).
    """

    __slots__ = ("_workers",)

    def __init__(self) -> None:
        self._workers: dict[str, WorkerTermState] = {}

    # -- Explicit accessors (preferred for new code) -----------------------

    def get(self, worker_id: str) -> WorkerTermState | None:
        """Return the state for *worker_id* or ``None`` if unknown."""
        return self._workers.get(worker_id)

    def require(self, worker_id: str) -> WorkerTermState:
        """Return the state for *worker_id* or raise ``KeyError``."""
        st = self._workers.get(worker_id)
        if st is None:
            raise KeyError(worker_id)
        return st

    def put(self, worker_id: str, state: WorkerTermState) -> None:
        """Insert or replace the state for *worker_id*."""
        self._workers[worker_id] = state

    def setdefault(self, worker_id: str, state: WorkerTermState) -> WorkerTermState:
        """Return the existing state for *worker_id*, or insert *state* and return it."""
        return self._workers.setdefault(worker_id, state)

    def pop(self, worker_id: str) -> WorkerTermState | None:
        """Remove and return the state for *worker_id*, or ``None`` if absent."""
        return self._workers.pop(worker_id, None)

    def discard(self, worker_id: str) -> bool:
        """Remove *worker_id* if present; return True if removed."""
        return self._workers.pop(worker_id, None) is not None

    def contains(self, worker_id: str) -> bool:
        """Return True if *worker_id* is registered."""
        return worker_id in self._workers

    def all(self) -> list[WorkerTermState]:
        """Return a snapshot list of all registered worker states."""
        return list(self._workers.values())

    def keys(self) -> list[str]:
        """Return a snapshot list of all registered worker ids."""
        return list(self._workers.keys())

    def items(self) -> list[tuple[str, WorkerTermState]]:
        """Return a snapshot list of (worker_id, state) tuples."""
        return list(self._workers.items())

    # -- Dunder convenience ------------------------------------------------

    def __len__(self) -> int:
        return len(self._workers)

    def __iter__(self) -> Iterator[str]:
        return iter(self._workers)

    def __contains__(self, worker_id: object) -> bool:
        return worker_id in self._workers


__all__ = ["WorkerRegistry"]
