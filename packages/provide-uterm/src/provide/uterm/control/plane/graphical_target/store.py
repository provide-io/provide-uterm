#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.control.plane.graphical_target.types import GraphicalTargetRecord


class GraphicalTargetStore(Protocol):
    """Transaction-bound persistence for graphical-target definitions.

    Tenant isolation is NOT enforced here: the store is a dumb row layer and
    the caller already holds a scope derived from the authenticated principal.
    Filtering by tenant at this level would silently double-gate reads and hide
    scope bugs from the registry's own tests.
    """

    async def put_graphical_target(self, record: GraphicalTargetRecord) -> None: ...

    async def get_graphical_target(self, target_id: str) -> GraphicalTargetRecord | None: ...

    async def list_graphical_targets(self) -> list[GraphicalTargetRecord]: ...

    async def delete_graphical_target(self, target_id: str) -> bool: ...
