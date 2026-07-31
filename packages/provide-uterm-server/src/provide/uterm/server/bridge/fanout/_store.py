#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FanOutStore protocol and InMemoryFanOutStore implementation."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.server.bridge.fanout._models import FanOutGroup


class FanOutStore(Protocol):
    """Protocol for fan-out group persistence backends."""

    async def save(self, group: FanOutGroup) -> None:
        """Persist a group, creating or replacing any existing entry with the same group_id."""
        ...

    async def get(self, group_id: str) -> FanOutGroup | None:
        """Return the group with the given ID, or None if not found."""
        ...

    async def delete(self, group_id: str) -> None:
        """Remove the group with the given ID. No-op if the group does not exist."""
        ...

    async def list_for_principal(self, principal: str) -> list[FanOutGroup]:
        """Return all groups where principal is the creator or appears in grants."""
        ...

    async def grant_access(self, group_id: str, grantee: str, creator: str) -> bool:
        """Atomically grant access when *creator* still owns the group."""
        ...


class InMemoryFanOutStore:
    """Ephemeral in-memory store. Groups are lost on restart."""

    def __init__(self) -> None:
        self._groups: dict[str, FanOutGroup] = {}

    async def save(self, group: FanOutGroup) -> None:
        """Persist a group, creating or replacing any existing entry with the same group_id."""
        self._groups[group.group_id] = self._clone(group)

    async def get(self, group_id: str) -> FanOutGroup | None:
        """Return the group with the given ID, or None if not found."""
        group = self._groups.get(group_id)
        return None if group is None else self._clone(group)

    async def delete(self, group_id: str) -> None:
        """Remove the group with the given ID. No-op if the group does not exist."""
        self._groups.pop(group_id, None)

    async def list_for_principal(self, principal: str) -> list[FanOutGroup]:
        """Return all groups where principal is the creator or appears in grants."""
        return [
            self._clone(group)
            for group in self._groups.values()
            if group.created_by == principal or principal in group.grants
        ]

    async def grant_access(self, group_id: str, grantee: str, creator: str) -> bool:
        """Atomically add a grant without exposing a mutable store record."""
        group = self._groups.get(group_id)
        if group is None or group.created_by != creator:
            return False
        if grantee not in group.grants:
            group.grants.append(grantee)
        return True

    @staticmethod
    def _clone(group: FanOutGroup) -> FanOutGroup:
        """Copy every mutable field across the store trust boundary."""
        return replace(group, worker_ids=list(group.worker_ids), grants=list(group.grants))
