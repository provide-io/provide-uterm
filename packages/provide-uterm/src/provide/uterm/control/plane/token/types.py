#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionTokenRecord:
    session_id: str
    token_kind: str
    token_value: str
    created_at: float
    expires_at: float | None
    revoked_at: float | None = None


@dataclass(frozen=True, slots=True)
class ResumeTokenRecord:
    token_value: str
    session_id: str
    role: str
    created_at: float
    expires_at: float
    was_hijack_owner: bool = False
    revoked_at: float | None = None
