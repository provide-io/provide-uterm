#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.control.plane.graphical_target.types import GraphicalTargetRecord
    from provide.uterm.control.plane.memory.transaction import MemoryState, MemoryTransaction


class MemoryGraphicalTargetStore:
    def __init__(self, state: MemoryState, tx: MemoryTransaction) -> None:
        self._state = state
        self._tx = tx

    async def put_graphical_target(self, record: GraphicalTargetRecord) -> None:
        self._state.graphical_targets[record.target_id] = record

    async def get_graphical_target(self, target_id: str) -> GraphicalTargetRecord | None:
        return self._state.graphical_targets.get(target_id)

    async def list_graphical_targets(self) -> list[GraphicalTargetRecord]:
        # Sorted by target_id so the memory and SQLite backends agree on order;
        # the SQLite store gets it from ORDER BY, dict order is insertion order.
        return [self._state.graphical_targets[key] for key in sorted(self._state.graphical_targets)]

    async def delete_graphical_target(self, target_id: str) -> bool:
        return self._state.graphical_targets.pop(target_id, None) is not None
