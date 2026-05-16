#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FanOutStore protocol and InMemoryFanOutStore implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.bridge.fanout._models import FanOutGroup


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


class InMemoryFanOutStore:
    """Ephemeral in-memory store. Groups are lost on restart."""

    def __init__(self) -> None:
        self._groups: dict[str, FanOutGroup] = {}

    async def save(self, group: FanOutGroup) -> None:
        """Persist a group, creating or replacing any existing entry with the same group_id."""
        self._groups[group.group_id] = group

    async def get(self, group_id: str) -> FanOutGroup | None:
        """Return the group with the given ID, or None if not found."""
        return self._groups.get(group_id)

    async def delete(self, group_id: str) -> None:
        """Remove the group with the given ID. No-op if the group does not exist."""
        self._groups.pop(group_id, None)

    async def list_for_principal(self, principal: str) -> list[FanOutGroup]:
        """Return all groups where principal is the creator or appears in grants."""
        return [group for group in self._groups.values() if group.created_by == principal or principal in group.grants]
