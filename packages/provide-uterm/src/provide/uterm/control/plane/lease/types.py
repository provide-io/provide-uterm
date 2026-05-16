#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
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
