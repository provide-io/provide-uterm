from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.control.plane.lease.types import LeaseRecord


class LeaseStore(Protocol):
    async def put_lease(self, record: LeaseRecord) -> None: ...

    async def get_lease(self, session_id: str) -> LeaseRecord | None: ...

    async def clear_lease(self, session_id: str) -> None: ...
