from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    session_id: str
    hijack_id: str
    owner: str
    lease_expires_at: float
    created_at: float
    deleted_at: float | None = None
