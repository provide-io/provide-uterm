#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.control.plane.approval.types import ApprovalRecord
    from provide.uterm.control.plane.lease.types import LeaseRecord
    from provide.uterm.control.plane.session.types import SessionRecord
    from provide.uterm.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord


@dataclass(slots=True)
class MemoryState:
    session_tokens: dict[tuple[str, str], SessionTokenRecord] = field(default_factory=dict)
    resume_tokens: dict[str, ResumeTokenRecord] = field(default_factory=dict)
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = field(default_factory=dict)
    leases: dict[str, LeaseRecord] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryTransaction:
    state: MemoryState
    closed: bool = False
    _snapshot_session_tokens: dict[tuple[str, str], SessionTokenRecord] = field(init=False, repr=False)
    _snapshot_resume_tokens: dict[str, ResumeTokenRecord] = field(init=False, repr=False)
    _snapshot_sessions: dict[str, SessionRecord] = field(init=False, repr=False)
    _snapshot_approvals: dict[str, ApprovalRecord] = field(init=False, repr=False)
    _snapshot_leases: dict[str, LeaseRecord] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._snapshot_session_tokens = self.state.session_tokens.copy()
        self._snapshot_resume_tokens = self.state.resume_tokens.copy()
        self._snapshot_sessions = self.state.sessions.copy()
        self._snapshot_approvals = self.state.approvals.copy()
        self._snapshot_leases = self.state.leases.copy()

    async def commit(self) -> None:
        self.closed = True

    async def rollback(self) -> None:
        self.state.session_tokens.clear()
        self.state.session_tokens.update(self._snapshot_session_tokens)

        self.state.resume_tokens.clear()
        self.state.resume_tokens.update(self._snapshot_resume_tokens)

        self.state.sessions.clear()
        self.state.sessions.update(self._snapshot_sessions)

        self.state.approvals.clear()
        self.state.approvals.update(self._snapshot_approvals)

        self.state.leases.clear()
        self.state.leases.update(self._snapshot_leases)

        self.closed = True
