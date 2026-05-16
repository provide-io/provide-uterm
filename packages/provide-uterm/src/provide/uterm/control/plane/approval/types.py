#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ApprovalState = Literal["pending", "approved", "rejected"]


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    session_id: str
    command: str
    requested_by: str | None
    state: ApprovalState
    created_at: float
    resolved_at: float | None = None
    resolved_by: str | None = None
