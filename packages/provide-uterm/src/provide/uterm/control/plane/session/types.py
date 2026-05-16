#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LifecycleState = Literal["waiting", "running", "stopped", "error", "deleted"]
Visibility = Literal["public", "operator", "private"]


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    display_name: str
    connector_type: str
    owner: str | None
    visibility: Visibility
    lifecycle_state: LifecycleState
    created_at: float
    updated_at: float
    deleted_at: float | None = None
