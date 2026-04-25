from __future__ import annotations

from dataclasses import dataclass, field

from provide.terminal.control.plane.approval.types import ApprovalRecord
from provide.terminal.control.plane.lease.types import LeaseRecord
from provide.terminal.control.plane.session.types import SessionRecord
from provide.terminal.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord


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

    async def commit(self) -> None:
        self.closed = True

    async def rollback(self) -> None:
        self.closed = True
