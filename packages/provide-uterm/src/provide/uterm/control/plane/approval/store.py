#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.control.plane.approval.types import ApprovalRecord


class ApprovalStore(Protocol):
    async def put_approval(self, record: ApprovalRecord) -> None: ...

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    async def list_pending(self) -> list[ApprovalRecord]: ...
