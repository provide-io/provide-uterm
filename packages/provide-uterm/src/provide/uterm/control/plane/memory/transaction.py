#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    import asyncio
    from collections.abc import MutableMapping

    from provide.uterm.control.plane.approval.types import ApprovalRecord
    from provide.uterm.control.plane.lease.types import LeaseRecord
    from provide.uterm.control.plane.session.types import SessionRecord
    from provide.uterm.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord

_K = TypeVar("_K")
_V = TypeVar("_V")


@dataclass(slots=True)
class MemoryState:
    session_tokens: dict[tuple[str, str], SessionTokenRecord] = field(default_factory=dict)
    resume_tokens: dict[str, ResumeTokenRecord] = field(default_factory=dict)
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = field(default_factory=dict)
    leases: dict[str, LeaseRecord] = field(default_factory=dict)


def _copy_state(state: MemoryState) -> MemoryState:
    return MemoryState(
        session_tokens=state.session_tokens.copy(),
        resume_tokens=state.resume_tokens.copy(),
        sessions=state.sessions.copy(),
        approvals=state.approvals.copy(),
        leases=state.leases.copy(),
    )


def _merge_table(
    root: MutableMapping[_K, _V],
    snapshot: MutableMapping[_K, _V],
    working: MutableMapping[_K, _V],
) -> None:
    """Apply only this transaction's key-level changes onto the shared table."""
    for key in set(snapshot) | set(working):
        before = snapshot.get(key)
        after = working.get(key)
        if after == before:
            continue
        if key in working:
            root[key] = working[key]
        else:
            root.pop(key, None)


@dataclass(slots=True)
class MemoryTransaction:
    _root: MemoryState
    _lock: asyncio.Lock
    state: MemoryState = field(init=False)
    closed: bool = False
    _snapshot: MemoryState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._snapshot = _copy_state(self._root)
        self.state = _copy_state(self._root)

    async def commit(self) -> None:
        if self.closed:
            return
        async with self._lock:
            _merge_table(self._root.session_tokens, self._snapshot.session_tokens, self.state.session_tokens)
            _merge_table(self._root.resume_tokens, self._snapshot.resume_tokens, self.state.resume_tokens)
            _merge_table(self._root.sessions, self._snapshot.sessions, self.state.sessions)
            _merge_table(self._root.approvals, self._snapshot.approvals, self.state.approvals)
            _merge_table(self._root.leases, self._snapshot.leases, self.state.leases)
        self.closed = True

    async def rollback(self) -> None:
        self.closed = True
