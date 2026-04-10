#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Data models for the terminal fan-out feature."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class FanOutGroup:
    """A named group of worker sessions that receive broadcast input together."""

    group_id: str
    name: str
    worker_ids: list[str]
    created_by: str
    created_at: float
    mode: str = "parallel"
    stop_on_first_error: bool = False
    error_pattern: str | None = None
    quiesce_ms: int = 500
    max_response_ms: int = 10_000
    divergence_threshold: float = 0.8
    grants: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionFanOutResult:
    """Result from a single session after a fan-out send."""

    worker_id: str
    ok: bool
    output_delta: str | None
    elapsed_ms: int
    divergent: bool


@dataclass(slots=True)
class FanOutResult:
    """Aggregated result of a fan-out command sent to a group."""

    group_id: str
    send_id: str
    command: str
    sent_at: float
    results: list[SessionFanOutResult]
    divergent_sessions: list[str]
    failed_sessions: list[str]
